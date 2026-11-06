"""Деление HTML-сообщений Telegram без потери текста и разрыва разметки."""
from html import unescape
from html.parser import HTMLParser


def _units(text: str) -> int:
    # Консервативно считаем UTF-16: emoji вне BMP занимают две единицы.
    return len(text.encode("utf-16-le")) // 2


class _Tokens(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.lines = [[]]

    def handle_starttag(self, tag, attrs):
        self.lines[-1].append(("open", self.get_starttag_text(), tag))

    def handle_endtag(self, tag):
        self.lines[-1].append(("close", f"</{tag}>", tag))

    def handle_data(self, data):
        for part in data.splitlines(keepends=True):
            self.lines[-1].append(("text", part, part))
            if part.endswith(("\n", "\r")):
                self.lines.append([])

    def handle_entityref(self, name):
        raw = f"&{name};"
        self.lines[-1].append(("entity", raw, unescape(raw)))

    def handle_charref(self, name):
        self.handle_entityref("#" + name)


def split_html(text: str, limit: int = 3900) -> list[str]:
    """Лимит — по тексту после разбора HTML; строки по возможности остаются целыми.

    Длинную строку делим по символам, сущности оставляем целиком. Открытые теги
    закрываются в конце части и восстанавливаются в следующей, включая ссылки.
    """
    if limit < 2:
        raise ValueError("Лимит должен вмещать хотя бы один символ UTF-16")
    parser = _Tokens()
    parser.feed(text)
    parser.close()
    chunks, part, stack = [], [], []
    used = 0

    def flush():
        nonlocal part, used
        if used:
            chunks.append("".join(part) + "".join(f"</{tag}>" for tag, _ in reversed(stack)))
            part = [raw for _, raw in stack]
            used = 0

    for line in parser.lines:
        length = sum(_units(value) for kind, _, value in line if kind in ("text", "entity"))
        if used and length <= limit and used + length > limit:
            flush()
        for kind, raw, value in line:
            if kind == "open":
                if used == limit:
                    flush()
                stack.append((value, raw))
                part.append(raw)
            elif kind == "close":
                if not stack or stack[-1][0] != value:
                    raise ValueError("Неверная вложенность HTML-тегов")
                stack.pop()
                part.append(raw)
            else:
                atoms = [(raw, value)] if kind == "entity" else ((char, char) for char in raw)
                for atom, plain in atoms:
                    size = _units(plain)
                    if used + size > limit:
                        flush()
                    part.append(atom)
                    used += size
    if stack:
        raise ValueError("Незакрытый HTML-тег")
    if used:
        chunks.append("".join(part))
    return chunks or [text]
