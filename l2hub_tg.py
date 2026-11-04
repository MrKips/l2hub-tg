"""Бот группы L2Hub в Telegram: сам сообщает о новых версиях L2Hub, о событиях моста и выкладывает наработки.

Релизы и мост проверяет GitHub Actions раз в полчаса (.github/workflows/bot.yml): мост клонируется
по deploy key только на чтение. Архивы для Кирилла появляются на компьютере, поэтому outbox выкладывается
отсюда, командой outbox. Что уже отправлено, помнит state/seen.json (Actions коммитит его обратно).

Режимы:
  python l2hub_tg.py watch [--dry-run]   — релизы L2Hub и мост (это и запускает Actions)
  python l2hub_tg.py outbox [--dry-run]  — выложить наработки из outbox/ в «Наработки» (локально)
  python l2hub_tg.py init                 — запомнить текущее состояние без публикаций (первый запуск)
  python l2hub_tg.py topics               — показать id тем группы, которые бот видел (для .env)

Ключи — в .env рядом со скриптом (в git не попадает) или в секретах репозитория: TELEGRAM_BOT_TOKEN,
LLM_API_KEY, TG_CHAT_ID и id тем; в Actions ещё BRIDGE_DEPLOY_KEY.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

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
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, files=files, timeout=60)
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method}: {body.get('description')}")
    return body["result"]


def post(where: str, text: str) -> None:
    """Сообщение в тему группы. HTML-разметка Telegram; длинное — обрезается."""
    if len(text) > TG_LIMIT:
        text = text[:TG_LIMIT - 1] + "…"
    if DRY:
        log(f"[пробно → {where}]\n{text}\n")
        return
    data = {"chat_id": os.environ["TG_CHAT_ID"], "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}
    thread = topic(where)
    if thread:
        data["message_thread_id"] = thread
    tg("sendMessage", data)
    time.sleep(1.5)  # Telegram не любит пачки сообщений в одну группу


def post_file(where: str, path: Path, caption: str) -> None:
    if DRY:
        log(f"[пробно → {where}] файл {path.name} ({path.stat().st_size // 1024} КБ)\n{caption}\n")
        return
    data = {"chat_id": os.environ["TG_CHAT_ID"], "caption": caption[:1000], "parse_mode": "HTML"}
    thread = topic(where)
    if thread:
        data["message_thread_id"] = thread
    with path.open("rb") as f:
        tg("sendDocument", data, files={"document": (path.name, f)})
    time.sleep(1.5)


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
    news, tasks = [], []
    for row in changed:
        parts = row.split("\t")
        status, path = parts[0], parts[-1]
        if path.startswith("updates/") and status.startswith("A"):
            text = show(head, path)
            title = next((l[2:] for l in text.splitlines() if l.startswith("# ")), path)
            what = next((l.split(":**", 1)[1].strip() for l in text.splitlines() if l.startswith("**Что изменилось:**")), "")
            news.append(f"📰 <b>{esc(title)}</b>\n{esc(what[:500])}\n<a href=\"{BRIDGE_WEB}/{path}\">читать</a>")
        elif path.startswith("tasks/") and path.endswith(".md"):
            new, old = front(show(head, path)), front(show(last, path)) if not status.startswith("A") else {}
            name = Path(path).stem
            if not old:
                tasks.append(f"➕ <b>{esc(name)}</b> — {esc(new.get('title', ''))} ({esc(new.get('status', '?'))}, {esc(new.get('who', '—'))})")
            elif (old.get("status"), old.get("who")) != (new.get("status"), new.get("who")):
                tasks.append(f"🔁 <b>{esc(name)}</b>: {esc(old.get('status', '?'))} → {esc(new.get('status', '?'))}"
                             + (f" ({esc(new.get('who'))})" if new.get("who") not in (None, "—") else ""))
    for text in news:
        post("SYSTEM", text)
    if tasks:
        post("SYSTEM", f"🔧 Мост{(' · ' + esc(who)) if who else ''}:\n" + "\n".join(tasks[:25]) + (f"\n…и ещё {len(tasks) - 25}" if len(tasks) > 25 else ""))
    state["bridge"] = head
    save_state(state)


# ---------- Наработки (outbox) ----------

def watch_outbox() -> None:
    """В outbox кладут пару: архив и текст с тем же именем (тема.zip + тема.md). Бот выкладывает в тему «Наработки» и переносит в sent/."""
    OUTBOX.mkdir(exist_ok=True)
    for md in sorted(OUTBOX.glob("*.md")):
        archive = md.with_suffix(".zip")
        text = md.read_text("utf-8").strip()
        if archive.exists():
            caption = text.split("\n\n")[0][:900]
            post_file("WORK", archive, esc(caption))
            if len(text) > len(caption):
                post("WORK", esc(text))
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
    parser.add_argument("mode", choices=["watch", "outbox", "init", "topics"])
    parser.add_argument("--dry-run", action="store_true", help="ничего не отправлять, показать в журнале")
    args = parser.parse_args()
    DRY = args.dry_run
    state = load_state()
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
    failed = False
    for name, step in (("релизы", lambda: watch_releases(state)), ("мост", lambda: watch_bridge(state))):
        try:
            step()
        except Exception as exc:  # одна сломанная часть не мешает остальным
            log(f"{name}: ошибка: {exc}")
            failed = True
    if failed:
        sys.exit(1)  # красный прогон в Actions — GitHub пришлёт письмо


if __name__ == "__main__":
    main()
