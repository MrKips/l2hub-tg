"""Бот группы L2Hub в Telegram: сам сообщает о новых версиях L2Hub, о событиях моста и выкладывает наработки.

Релизы и мост проверяет GitHub Actions раз в полчаса (.github/workflows/bot.yml): мост клонируется
по deploy key только на чтение. Архивы для Кирилла появляются на компьютере, поэтому outbox выкладывается
отсюда, командой outbox. Что уже отправлено, помнит state/seen.json (Actions коммитит его обратно).

Режимы:
  python l2hub_tg.py watch [--dry-run]   — релизы L2Hub, мост и раз в день сводка (это и запускает Actions)
  python l2hub_tg.py digest [--dry-run]  — сводка по мосту прямо сейчас
  python l2hub_tg.py outbox [--dry-run]  — выложить наработки из outbox/ в «Наработки» (локально)
  python l2hub_tg.py init                 — запомнить текущее состояние без публикаций (первый запуск)
  python l2hub_tg.py topics               — показать id тем группы, которые бот видел (для .env)

Ключи — в .env рядом со скриптом (в git не попадает) или в секретах репозитория: TELEGRAM_BOT_TOKEN,
LLM_API_KEY, TG_CHAT_ID и id тем; в Actions ещё BRIDGE_DEPLOY_KEY.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from telegram_html import split_html
from system_text import describe
import coordination

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state" / "seen.json"
OUTBOX = ROOT / "outbox"
SENT = OUTBOX / "sent"

L2HUB_REPO = "https://github.com/kraffty4/L2Hub"
L2HUB_RAW = "https://raw.githubusercontent.com/kraffty4/L2Hub"
BRIDGE_DIR = Path(os.environ.get("BRIDGE_DIR", r"D:\Games\L2Hub-bridge"))
BRIDGE_WEB = "https://github.com/kraffty4/L2Hub-bridge/blob/main"

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-flash")
TG_LIMIT = 3900
TG_SEND_INTERVAL = 3.2  # менее 20 сообщений в минуту на всю группу, включая разные темы
TG_RATE_RETRIES = 3
DRY = False


def log(*parts) -> None:
    print(datetime.now().strftime("%H:%M:%S"), *parts, flush=True)


def load_dotenv() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text("utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"'))


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text("utf-8"))
    return {}


def save_state(state: dict) -> None:
    if DRY:
        return
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")


# ---------- Telegram ----------

def topic(name: str) -> int | None:
    """Id темы группы из .env (TG_TOPIC_UPDATES и т. п.); нет — общая лента."""
    value = os.environ.get(f"TG_TOPIC_{name}", "").strip()
    return int(value) if value.lstrip("-").isdigit() else None


def tg(method: str, data: dict, files: dict | None = None) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("нет TELEGRAM_BOT_TOKEN в .env")
    streams = [item[1] if isinstance(item, tuple) else item for item in (files or {}).values()]
    positions = [(stream, stream.tell()) for stream in streams]
    for attempt in range(TG_RATE_RETRIES + 1):
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, files=files, timeout=60)
        body = r.json()
        if body.get("ok"):
            return body["result"]
        if (body.get("error_code") == 429 or r.status_code == 429) and attempt < TG_RATE_RETRIES:
            delay = max(1, int((body.get("parameters") or {}).get("retry_after", 1))) + 1
            log(f"Telegram: ограничение частоты, жду {delay} с и повторяю отклонённый запрос")
            time.sleep(delay)
            for stream, position in positions:
                stream.seek(position)
            continue
        raise RuntimeError(f"Telegram {method}: {body.get('description')}")


def post(where: str, text: str) -> None:
    """Сообщение в тему группы; длинное делится с сохранением HTML и всего текста."""
    for chunk in split_html(text, TG_LIMIT):
        if DRY:
            log(f"[пробно → {where}]\n{chunk}\n")
            continue
        data = {"chat_id": os.environ["TG_CHAT_ID"], "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        thread = topic(where)
        if thread:
            data["message_thread_id"] = thread
        tg("sendMessage", data)
        time.sleep(TG_SEND_INTERVAL)


def post_file(where: str, path: Path, caption: str) -> None:
    if DRY:
        log(f"[пробно → {where}] файл {path.name} ({path.stat().st_size // 1024} КБ)\n{caption}\n")
        return
    data = {"chat_id": os.environ["TG_CHAT_ID"], "caption": split_html(caption, 1000)[0], "parse_mode": "HTML"}
    thread = topic(where)
    if thread:
        data["message_thread_id"] = thread
    with path.open("rb") as f:
        tg("sendDocument", data, files={"document": (path.name, f)})
    time.sleep(TG_SEND_INTERVAL)


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- DeepSeek ----------

def ask_llm(system: str, user: str, max_tokens: int = 1500) -> str | None:
    """Короткий пересказ. Нет ключа или модель молчит — None, и бот шлёт сырые строки."""
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        return None
    body = {"model": LLM_MODEL, "temperature": 0.3, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if "deepseek.com" in LLM_BASE_URL:
        body["thinking"] = {"type": "disabled"}  # для пересказа рассуждение не нужно
    for attempt in range(3):
        try:
            r = requests.post(f"{LLM_BASE_URL}/chat/completions", json=body, timeout=120, headers={"Authorization": f"Bearer {key}"})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except (requests.RequestException, KeyError, ValueError) as exc:
            log("  нейросеть:", exc)
            time.sleep(5 * (attempt + 1))
    return None


RELEASE_PROMPT = (
    "Ты пишешь заметку о новой версии L2Hub для разработчиков проекта в Telegram. L2Hub — лаунчер локальных серверов Lineage II "
    "с ботами-охотниками. Тебе дают строки CHANGELOG за эту версию. Напиши по-русски 3–7 коротких пунктов «• …»: что появилось и что "
    "исправлено, простыми словами, без воды и без внутренних имён файлов, если без них понятно. Ничего не выдумывай сверх строк. "
    "Только пункты, без заголовка и вступления, не длиннее 1500 символов."
)


# ---------- Релизы L2Hub ----------

def version_key(tag: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", tag))


def release_tags() -> list[str]:
    out = subprocess.run(["git", "ls-remote", "--tags", L2HUB_REPO], capture_output=True, text=True, timeout=60, check=True).stdout
    tags = {line.split("refs/tags/")[1].removesuffix("^{}") for line in out.splitlines() if "refs/tags/v" in line}
    return sorted(tags, key=version_key)


def changelog_lines(tag: str, after: str | None) -> list[str]:
    """Строки CHANGELOG вида «- **0.5.179** · …» новее прошлого тега и не новее этого."""
    r = requests.get(f"{L2HUB_RAW}/{tag}/CHANGELOG.md", timeout=60)
    r.raise_for_status()
    top, low = version_key(tag), version_key(after) if after else (0,)
    lines = []
    for line in r.text.splitlines():
        m = re.match(r"- \*\*(\d+\.\d+\.\d+)\*\*", line)
        if m and low < version_key(m.group(1)) <= top:
            lines.append(line[2:])
    return lines


def watch_releases(state: dict) -> None:
    tags = release_tags()
    last = state.get("release")
    fresh = [t for t in tags if not last or version_key(t) > version_key(last)]
    for tag in fresh:
        lines = changelog_lines(tag, last)
        summary = ask_llm(RELEASE_PROMPT, "\n".join(lines)) if lines else None
        if not summary:
            summary = "\n".join("• " + re.sub(r"\*\*", "", l)[:300] for l in lines[:8]) or "• подробности — в CHANGELOG"
        post("UPDATES", f"🚀 <b>Вышла L2Hub {esc(tag)}</b>\n\n{esc(summary)}\n\n<a href=\"{L2HUB_REPO}/blob/{tag}/CHANGELOG.md\">CHANGELOG</a> · "
                        f"<a href=\"{L2HUB_REPO}/releases\">релизы</a>")
        last = tag
        state["release"] = tag
        save_state(state)


# ---------- Мост ----------

def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(BRIDGE_DIR), *args], capture_output=True, text=True, encoding="utf-8", timeout=120, check=True).stdout


def front(text: str) -> dict:
    fields = {}
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip()] = value.strip()
    return fields


def show(rev: str, path: str) -> str:
    try:
        return git("show", f"{rev}:{path}")
    except subprocess.CalledProcessError:
        return ""


def watch_bridge(state: dict) -> None:
    git("pull", "--rebase", "-q")
    head = git("rev-parse", "HEAD").strip()
    last = state.get("bridge")
    if last == head:
        return
    if not last:
        state["bridge"] = head
        save_state(state)
        return
    changed = git("diff", "--name-status", last, head).splitlines()
    who = ", ".join(dict.fromkeys(a for a in git("log", "--format=%an", f"{last}..{head}").splitlines() if a.strip()))
    news, tasks, task_cards = [], [], []
    for row in changed:
        parts = row.split("\t")
        status, path = parts[0], parts[-1]
        if path.startswith("updates/") and status.startswith("A"):
            text = show(head, path)
            title = next((l[2:] for l in text.splitlines() if l.startswith("# ")), path)
            what = next((l.split(":**", 1)[1].strip() for l in text.splitlines() if l.startswith("**Что изменилось:**")), "")
            canonical = f"📰 <b>{esc(title)}</b>\n{esc(what[:500])}\n<a href=\"{BRIDGE_WEB}/{path}\">читать</a>"
            news.append({"source": {"kind": "news", "title": title, "details": text[:7000]}, "path": path, "canonical": canonical})
        elif path.startswith("tasks/") and path.endswith(".md"):
            new, old = front(show(head, path)), front(show(last, path)) if not status.startswith("A") else {}
            name = Path(path).stem
            if not old:
                tasks.append(f"➕ <b>{esc(name)}</b> — {esc(new.get('title', ''))} ({esc(new.get('status', '?'))}, {esc(new.get('who', '—'))})")
                task_cards.append((name, new))
            elif (old.get("status"), old.get("who"), old.get("release")) != (new.get("status"), new.get("who"), new.get("release")):
                tasks.append(f"🔁 <b>{esc(name)}</b>: {esc(old.get('status', '?'))} → {esc(new.get('status', '?'))}"
                             + (f" ({esc(new.get('who'))})" if new.get("who") not in (None, "—") else ""))
                task_cards.append((name, new))

    shown_cards = task_cards[:10]
    copies = describe([item["source"] for item in news] + [{"kind": "task", "title": card.get("title", name)} for name, card in shown_cards], ask_llm)

    def deliver(text: str, canonical: str) -> None:
        # Сохраняем каждую принятую новость: после сбоя продолжаем, а не повторяем всю пачку.
        # Ключ от исходных данных: другой пересказ при повторе не создаёт дубль.
        key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if key in state.get("bridge_sent", []):
            return
        post("SYSTEM", text)
        state.setdefault("bridge_sent", []).append(key)
        save_state(state)

    for item, copy in zip(news, copies):
        text = f"📰 <b>{esc(copy['title'])}</b>\n\n{esc(copy['summary'])}\n\n<a href=\"{BRIDGE_WEB}/{item['path']}\">Подробности и проверка</a>"
        deliver(text, item["canonical"])
    if tasks:
        lines = ["🛠 <b>Что с работой команды</b>"]
        for (name, card), copy in zip(shown_cards, copies[len(news):]):
            author = WHO.get(card.get("who", ""), card.get("who", "автор не указан"))
            scope = f" · {card['chronicle']}" if card.get("chronicle") else ""
            lines += ["", f"• <b>{esc(copy['title'])}</b>", esc(task_status(card, state.get("release", ""))),
                      f"{esc(author + scope)} · <a href=\"{BRIDGE_WEB}/tasks/{name}.md\">подробнее</a>"]
        if len(task_cards) > len(shown_cards):
            lines += ["", f"Ещё изменений в списке задач: {len(task_cards) - len(shown_cards)}."]
        lines += ["", f"<a href=\"{BRIDGE_WEB}/tasks\">Все задачи команды</a>"]
        canonical = f"🔧 Мост{(' · ' + esc(who)) if who else ''}:\n" + "\n".join(tasks[:25]) + (f"\n…и ещё {len(tasks) - 25}" if len(tasks) > 25 else "")
        deliver("\n".join(lines), canonical)
    state["bridge"] = head
    state.pop("bridge_sent", None)
    save_state(state)


# ---------- Сводка по мосту ----------

WHO = {"selaquik": "Сергей", "kraffty": "Кирилл", "MrKips": "Илья"}
DIGEST_TZ = timezone(timedelta(hours=5))  # Ташкент
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "20"))


def digest_day() -> datetime:
    """День последней сводки: до DIGEST_HOUR это ещё вчерашняя (её догоняем, если Actions проспал вечер)."""
    now = datetime.now(DIGEST_TZ)
    return now if now.hour >= DIGEST_HOUR else now - timedelta(days=1)
VERSION_RE = re.compile(r"v?\d+\.\d+\.\d+$")


def bridge_tasks() -> dict[str, dict]:
    tasks = {}
    for path in sorted((BRIDGE_DIR / "tasks").glob("*.md")):
        fields = front(path.read_text("utf-8"))
        fields.setdefault("title", path.stem)
        tasks[path.stem] = fields
    return tasks


def digest_buckets(tasks: dict, latest: str) -> dict[str, list[str]]:
    """Четыре корзины: в сборке, принято (выйдет позже), в работе, сделано и ждёт приёмки.
    «В сборке» и «принято» — только по строке release: в шапке задачи, без догадок по тексту."""
    out = {"released": [], "accepted": [], "work": [], "done": []}
    for name, t in tasks.items():
        release = t.get("release", "").strip()
        if VERSION_RE.match(release):
            out["released" if version_key(release) <= version_key(latest) else "accepted"].append(name)
        elif t.get("status") == "в работе":
            out["work"].append(name)
        elif t.get("status") == "готово":
            out["done"].append(name)
    return out


def task_status(t: dict, latest: str) -> str:
    """Готовность по полям моста и выпущенному тегу; нейросеть её не определяет."""
    release = t.get("release", "").strip()
    if VERSION_RE.fullmatch(release):
        if VERSION_RE.fullmatch(latest):
            return f"✅ Включено в опубликованную версию {release}." if version_key(release) <= version_key(latest) else f"📦 Принято для версии {release}; её выпуска ещё ждём."
        return f"Назначена версия {release}; её публикация пока не проверена."
    return {"в работе": "🛠 Над этим сейчас работают.", "готово": "🧪 Автор закончил работу; включение в общий выпуск пока не подтверждено.",
            "план": "💡 Пока это план, работа ещё не начата.", "свободна": "🙋 Задача ждёт исполнителя.",
            "брошена": "⏸ Работу по этой теме остановили."}.get(t.get("status"), "Текущий статус автором не указан.")


def task_line(name: str, t: dict, title: str | None = None) -> str:
    who = WHO.get(t.get("who", ""), t.get("who", "—"))
    release = t.get("release", "").strip()
    scope = f" · {esc(t['chronicle'])}" if t.get("chronicle") else ""
    return f"• {esc(title or t['title'])} — {esc(who)}{scope}" + (f" ({esc(release)})" if VERSION_RE.match(release) else "") + f" <a href=\"{BRIDGE_WEB}/tasks/{name}.md\">подробнее</a>"


def coordination_task(t: dict) -> bool:
    title = t.get("title", "").lower()
    return any(part in title for part in ("реестр", "порядок работы", "все незавершённые", "передача полного исходного", "локальных id", "сопровождение gracia"))


def post_digest(state: dict) -> None:
    tasks = bridge_tasks()
    latest = release_tags()[-1]
    now = digest_buckets(tasks, latest)
    prev = state.get("digest", {})
    new_released = [n for n in now["released"] if n not in prev.get("released", [])] if prev else []
    new_done = [n for n in now["done"] if n not in prev.get("done", [])] if prev else []
    day = digest_day()
    game_work = [n for n in now["work"] if not coordination_task(tasks[n])]
    sections = [("🆕 <b>Появилось в выпущенных версиях</b> — доступно после обновления:", new_released),
                ("📦 <b>Принято, но ещё не выпущено:</b>", now["accepted"]),
                ("🛠 <b>Над чем сейчас работают:</b>", game_work),
                ("🧪 <b>Авторы закончили, включение в общий выпуск ещё не подтверждено:</b>", new_done)]
    shown = list(dict.fromkeys(n for _, names in sections for n in names[:8]))
    copies = dict(zip(shown, describe([{"kind": "task", "title": tasks[n]["title"]} for n in shown], ask_llm)))
    lines = [f"📋 <b>Что нового и над чем работают</b> · {day:%d.%m}", f"Последняя опубликованная версия L2Hub: <b>{esc(latest)}</b>."]
    for heading, names in sections:
        if names:
            lines += ["", heading, *(task_line(n, tasks[n], copies[n]["title"]) for n in names[:8])]
            if len(names) > 8:
                lines.append(f"Ещё тем в этом разделе: {len(names) - 8}. Полный список — по ссылке ниже.")
    coordination = len(now["work"]) - len(game_work)
    if coordination:
        lines += ["", f"Ещё {coordination} записей — организация работы и учёт задач."]
    per = {}
    for n in now["done"]:
        who = WHO.get(tasks[n].get("who", ""), tasks[n].get("who", "—"))
        per[who] = per.get(who, 0) + 1
    lines += ["", f"Всего у авторов готово тем без подтверждённого общего выпуска: {len(now['done'])}." + (" По авторам: " + ", ".join(f"{esc(w)} — {c}" for w, c in per.items()) + "." if per else "")]
    if not now["released"] and not now["accepted"]:
        lines += ["", "<i>По записям команды пока нельзя подтвердить, какие из этих тем вошли в общий выпуск.</i>"]
    lines += ["", f"<a href=\"{BRIDGE_WEB}/tasks\">Все задачи и технические подробности</a>"]
    post("SYSTEM", "\n".join(lines))
    state["digest"] = {"day": f"{day:%Y-%m-%d}", "released": now["released"], "done": now["done"]}
    save_state(state)


def maybe_digest(state: dict) -> None:
    """Раз в день, после DIGEST_HOUR по Ташкенту; пропущенную вечером — первым же запуском после."""
    if state.get("digest", {}).get("day", "") < f"{digest_day():%Y-%m-%d}":
        post_digest(state)


# ---------- Общие планы (не меняют карточки моста) ----------

def plans_link(thread: int, message: int) -> str:
    chat = os.environ["TG_CHAT_ID"]
    if not chat.startswith("-100"):
        raise RuntimeError("Обзор планов требует супергруппу Telegram")
    return f"https://t.me/c/{chat[4:]}/{thread}/{message}"


def sync_plans(state: dict) -> None:
    thread = topic("PLANS")
    if not thread:
        return
    cards, source = coordination.read_snapshot(BRIDGE_DIR)
    latest = state.get("release", "")
    route = hashlib.sha256(f"{os.environ['TG_CHAT_ID']}:{thread}".encode()).hexdigest()
    current = state.get("plans", {})
    if (current.get("route") == route and current.get("tasks_tree") == source["tasks_tree"]
            and current.get("release") == latest and current.get("view_version") == coordination.VIEW_VERSION):
        return
    data = coordination.report(cards, source, latest)
    parts = coordination.sections(data)
    if DRY:
        for _, text in parts:
            post("PLANS", text)
        log("[пробно → PLANS] обзор будет обновлён без повторной рассылки неизменившихся разделов")
        return
    if current.get("route") != route:
        current = {"route": route, "pages": {}}
        state["plans"] = current

    def update(key: str, number: int, text: str) -> int:
        pages = current["pages"].setdefault(key, [])
        while len(pages) <= number:
            pages.append({})
        page = pages[number]
        checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if page.get("id") and page.get("hash") == checksum:
            return page["id"]
        payload = {"chat_id": os.environ["TG_CHAT_ID"], "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        if page.get("id"):
            try:
                tg("editMessageText", dict(payload, message_id=page["id"]))
            except RuntimeError as exc:
                if "message to edit not found" in str(exc).lower():
                    page.pop("id", None)
                elif "message is not modified" not in str(exc).lower():
                    raise
        if not page.get("id"):
            result = tg("sendMessage", dict(payload, message_thread_id=thread, disable_notification="true"))
            page["id"] = result["message_id"]
        page["hash"] = checksum
        save_state(state)
        time.sleep(TG_SEND_INTERVAL)
        return page["id"]

    links = {}
    for key, text in parts:
        chunks = split_html(text, TG_LIMIT - 100)
        messages = [update(key, i, chunk) for i, chunk in enumerate(chunks)]
        links[key] = [plans_link(thread, message) for message in messages]
        for i in range(len(chunks), len(current["pages"][key])):
            update(key, i, f'Раздел стал короче. <a href="{links[key][0]}">Актуальный список</a>.')
    # Если раздел опустел, старые записи больше не должны выглядеть актуальными.
    for key, pages in list(current["pages"].items()):
        if key not in links and key != "index":
            for i in range(len(pages)):
                update(key, i, f'В этом разделе сейчас нет задач. <a href="{coordination.WEB}/tasks">Карточки моста</a>.')
    update("index", 0, coordination.overview(data, links))
    current.update({"tasks_tree": source["tasks_tree"], "release": latest, "view_version": coordination.VIEW_VERSION})
    save_state(state)


# ---------- Наработки (outbox) ----------

def watch_outbox() -> None:
    """В outbox кладут пару: архив и текст с тем же именем (тема.zip + тема.md). Бот выкладывает в тему «Наработки» и переносит в sent/."""
    OUTBOX.mkdir(exist_ok=True)
    for md in sorted(OUTBOX.glob("*.md")):
        archive = md.with_suffix(".zip")
        text = md.read_text("utf-8").strip()
        if archive.exists():
            first, _, rest = text.partition("\n\n")
            post_file("WORK", archive, esc(first[:900]))
            if rest.strip():  # остальное — отдельным сообщением, без повтора подписи
                post("WORK", esc(rest.strip()))
        else:
            post("WORK", esc(text))
        if not DRY:
            SENT.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
            for f in (md, archive):
                if f.exists():
                    f.rename(SENT / f"{stamp}_{f.name}")


# ---------- Команды ----------

def show_topics() -> None:
    """Id тем: бот должен быть в группе и видеть сообщения в каждой теме (написать в тему что-нибудь после добавления бота)."""
    seen = {}
    for upd in tg("getUpdates", {"timeout": 0}):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat", {})
        thread = msg.get("message_thread_id")
        name = ((msg.get("reply_to_message") or {}).get("forum_topic_created") or msg.get("forum_topic_created") or {}).get("name", "")
        seen[(chat.get("id"), thread)] = (chat.get("title"), name)
    for (chat, thread), (title, name) in seen.items():
        print(f"chat_id={chat} ({title})  тема={thread} {name}")


def main() -> None:
    global DRY
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv()
    parser = argparse.ArgumentParser(description="Бот группы L2Hub в Telegram")
    parser.add_argument("mode", choices=["watch", "digest", "outbox", "init", "topics", "plans"])
    parser.add_argument("--dry-run", action="store_true", help="ничего не отправлять, показать в журнале")
    parser.add_argument("--json", action="store_true", help="plans: структурированный снимок для агентов")
    parser.add_argument("--find", default="", help="plans: поиск существующих работ")
    parser.add_argument("--chronicle", default="", help="plans: целевая хроника первой; источники других хроник сохраняются")
    args = parser.parse_args()
    DRY = args.dry_run
    state = load_state()
    if args.mode == "plans":
        git("fetch", "origin", "-q")  # обновляем ref без переключения и изменения чужой рабочей папки
        cards, source = coordination.read_snapshot(BRIDGE_DIR)
        latest = state.get("release", "")
        try:
            latest = release_tags()[-1]
        except (subprocess.SubprocessError, IndexError):
            source["release_from_last_run"] = True
        data = coordination.report(cards, source, latest, args.find, args.chronicle)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(f"Карточек: {len(data['cards'])}. Источник: {source['commit']} ({source['ref']}).")
            for _, text in coordination.sections(data):
                print(text + "\n")
            print(data["notice"])
        return
    if args.mode == "topics":
        show_topics()
        return
    if args.mode == "init":
        state["release"] = release_tags()[-1]
        state["bridge"] = git("rev-parse", "HEAD").strip()
        save_state(state)
        log("запомнил:", state)
        return
    if args.mode == "outbox":
        watch_outbox()
        return
    if args.mode == "digest":
        post_digest(state)
        return
    failed = False
    for name, step in (("релизы", lambda: watch_releases(state)), ("мост", lambda: watch_bridge(state)),
                       ("сводка", lambda: maybe_digest(state)), ("планы", lambda: sync_plans(state))):
        try:
            step()
        except Exception as exc:  # одна сломанная часть не мешает остальным
            log(f"{name}: ошибка: {exc}")
            failed = True
    if failed:
        sys.exit(1)  # красный прогон в Actions — GitHub пришлёт письмо


if __name__ == "__main__":
    main()
