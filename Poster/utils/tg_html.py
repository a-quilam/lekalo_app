# utils/tg_html.py
"""
Безопасный HTML: форматирование Telegram (MessageEntity) → HTML и разбиение.

Внутреннее представление форматируемых полей поста — безопасный HTML:
обычный текст экранируется, разметка исходного сообщения воспроизводится
стандартными тегами Telegram. Один и тот же HTML уходит в превью, в готовый
пост и хранится в черновике (колонка Draft.formats) — двойного экранирования
нет, превью дословно совпадает с финальной отправкой.

Смещения MessageEntity заданы в 16-битных код-юнитах UTF-16 (как в
Telegram), поэтому разбор ведётся по utf-16-байтам — так же, как в
`Message._parse_html` python-telegram-bot, — а не по символам Python:
эмодзи и прочие широкие символы не сдвигают границы форматирования.

Разница с `Message._parse_html` PTB (сознательная): значения атрибутов
(href, emoji-id, language) экранируются — иначе кавычка в ссылке ломает
всю разметку сообщения; spoilter и pre+language пишутся в документированном
Telegram виде.
"""

import re
from collections.abc import Sequence
from datetime import datetime
from html import escape as _escape
from html import unescape as _unescape

from telegram import MessageEntity

from utils.parsers import APP_TZ


class FormattedText(str):
    """
    Пользовательский текст вместе с его безопасным HTML (U1b).

    Строковое значение — обычный текст: по нему считаются длина, пустота и
    валидность. `html` — результат `entities_to_html`, то есть ровно то, что
    уйдёт в Telegram. Так форматирование не теряется между захватом ввода и
    записью в черновик.
    """

    html: str

    def __new__(cls, text: str, html: str) -> "FormattedText":
        instance = super().__new__(cls, text)
        instance.html = html
        return instance


class SafeHtml(str):
    """
    HTML значения, уже прошедший allow-list (U1b).

    Подкласс `str`, чтобы значение оставалось строкой для существующей
    валидации и сравнений, но компилятор шаблона видел маркер и НЕ экранировал
    разметку повторно. Создаётся только функцией `sanitize_html` этого
    модуля, не пользовательским вводом.
    """


def capture_text(
    text: str | None, entities: Sequence[MessageEntity] | None
) -> FormattedText:
    """Текст сообщения Telegram вместе с его разметкой (точка захвата U1b)."""
    plain = text or ""
    return FormattedText(plain, entities_to_html(plain, entities))


def capture_message_text(message: object) -> FormattedText:
    """
    Захват пользовательского ввода из сообщения Telegram.

    Заменяет плоское `message.text`: entities переводятся в безопасный HTML
    тем же кодировщиком, что и раньше для превью старых сценариев.
    """
    if message is None:
        return FormattedText("", "")
    return capture_text(
        getattr(message, "text", None),
        getattr(message, "entities", None),
    )


def escape_html(text: str) -> str:
    """
    HTML-экранирование текста и значений атрибутов: &, <, >, ", '.

    Один экранировщик для текстовых узлов и атрибутов: значение в кавычках
    обязано быть экранировано, а Telegram одинаково разворачивает сущности
    и в тексте, и в атрибутах.
    """
    return _escape(text, quote=True)


def utf16_len(text: str) -> int:
    """Длина строки в 16-битных код-юнитах UTF-16 — единицах смещений Telegram."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def entities_to_html(text: str, entities: Sequence[MessageEntity] | None) -> str:
    """
    Текст сообщения с entities → безопасный HTML.

    Без entities — просто экранированный текст. Entities с битыми смещениями
    (нулевая длина, выход за конец текста) игнорируются: текст остаётся
    как есть, содержимое не теряется.
    """
    if not text or not entities:
        return escape_html(text)

    utf16_text = text.encode("utf-16-le", "surrogatepass")
    total_units = len(utf16_text) // 2
    sliced: dict[MessageEntity, str] = {}
    for entity in entities:
        if entity.length <= 0 or entity.offset < 0:
            continue
        if entity.offset + entity.length > total_units:
            continue
        start = entity.offset * 2
        sliced[entity] = utf16_text[start : start + entity.length * 2].decode(
            "utf-16-le"
        )

    if not sliced:
        return escape_html(text)
    return _parse_html(text, sliced)


def _parse_html(
    message_text: str,
    entities: dict[MessageEntity, str],
    *,
    offset: int = 0,
) -> str:
    """
    Рекурсивная сборка HTML: сортировка по смещению, вложенность entities,
    срезы по UTF-16 — алгоритм PTB `Message._parse_html`, но со
    экранированием значений атрибутов.
    """
    utf16_text = message_text.encode("utf-16-le", "surrogatepass")
    html_text = ""
    last_offset = 0
    sorted_entities = sorted(entities.items(), key=lambda item: item[0].offset)
    parsed: list[MessageEntity] = []

    for entity, entity_text in sorted_entities:
        if entity in parsed:
            continue
        nested = {
            nested_entity: nested_text
            for nested_entity, nested_text in sorted_entities
            if nested_entity.offset >= entity.offset
            and nested_entity.offset + nested_entity.length
            <= entity.offset + entity.length
            and nested_entity != entity
        }
        parsed.extend(nested.keys())

        if nested:
            inner = _parse_html(entity_text, nested, offset=entity.offset)
        else:
            inner = escape_html(entity_text)

        # Текст между предыдущей entities и текущей тоже экранируем
        html_text += escape_html(
            utf16_text[last_offset * 2 : (entity.offset - offset) * 2].decode(
                "utf-16-le"
            )
        )
        html_text += _wrap(entity, inner, entity_text)
        last_offset = entity.offset - offset + entity.length

    html_text += escape_html(utf16_text[last_offset * 2 :].decode("utf-16-le"))
    return html_text


def _wrap(entity: MessageEntity, inner: str, raw_text: str) -> str:
    """
    Тег для конкретного типа entity. raw_text — исходный текст entity
    (нужен URL как href). Вспомогательные типы без разметки Telegram
    (mention, hashtag, email, ...) возвращают просто текст — содержимое
    сохраняется.

    `date_time` (Bot API 10.0, PTB 22.8) деградирует в ЧИТАЕМЫЙ текст:
    Telegram не документирует `<tg-time>` как допустимый тег для
    parse_mode=HTML при отправке (`<tg-time unix="…" format="…">` есть
    только в обратном направлении — `Message.text_html`, то есть в
    сериализации уже полученной entity). Отправлять его «наугад» нельзя:
    неизвестный тег ломает разметку всего сообщения. Поэтому фиксируем
    предсказуемую деградацию: unix_time → локализованные дата/время по
    `date_time_format`, при неразбираемом формате — ISO; без unix_time
    остаётся исходный видимый текст.
    """
    kind = entity.type
    if kind == MessageEntity.BOLD:
        return f"<b>{inner}</b>"
    if kind == MessageEntity.ITALIC:
        return f"<i>{inner}</i>"
    if kind == MessageEntity.UNDERLINE:
        return f"<u>{inner}</u>"
    if kind == MessageEntity.STRIKETHROUGH:
        return f"<s>{inner}</s>"
    if kind == MessageEntity.SPOILER:
        return f"<tg-spoiler>{inner}</tg-spoiler>"
    if kind == MessageEntity.CODE:
        return f"<code>{inner}</code>"
    if kind == MessageEntity.PRE:
        if entity.language:
            # Документированный Telegram вид pre с языком
            language = escape_html(entity.language)
            return f'<pre><code class="language-{language}">' f"{inner}</code></pre>"
        return f"<pre>{inner}</pre>"
    if kind == MessageEntity.TEXT_LINK and entity.url:
        return f'<a href="{escape_html(entity.url)}">{inner}</a>'
    if kind == MessageEntity.URL:
        # Явная ссылка вместо надежды на автоссылку: так URL остаётся
        # кликабельным независимо от того, как клиент разберёт HTML
        return f'<a href="{escape_html(raw_text)}">{inner}</a>'
    if kind == MessageEntity.TEXT_MENTION and entity.user:
        return f'<a href="tg://user?id={entity.user.id}">{inner}</a>'
    if kind == MessageEntity.BLOCKQUOTE:
        return f"<blockquote>{inner}</blockquote>"
    if kind == MessageEntity.EXPANDABLE_BLOCKQUOTE:
        return f"<blockquote expandable>{inner}</blockquote>"
    if kind == MessageEntity.CUSTOM_EMOJI and entity.custom_emoji_id:
        emoji_id = escape_html(str(entity.custom_emoji_id))
        return f'<tg-emoji emoji-id="{emoji_id}">{inner}</tg-emoji>'
    if kind == MessageEntity.DATE_TIME:
        readable = format_date_time(entity.unix_time, entity.date_time_format)
        return escape_html(readable) if readable else inner
    return inner


# —— `date_time` entity → читаемый текст ———————————————————————————————

# Все допустимые флаги Bot API (`date-time entity formatting`).
_DATE_TIME_FLAGS = frozenset("rwdDtT")
_DATE_TIME_DEFAULT_FORMAT = "dt"

_WEEKDAYS_RU = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
_MONTHS_RU = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def format_date_time(
    unix_time: datetime | None,
    date_time_format: str | None,
    *,
    now: datetime | None = None,
) -> str | None:
    """
    Читаемое представление `date_time`-entity без тега `<tg-time>`.

    Возвращает None, если времени нет (тогда остаётся исходный текст).
    Формат разбирается по флагам Bot API: дата (`d`/`D`), время
    (`t`/`T`), день недели (`w`), относительность (`r`). Неизвестная
    строка формата — ISO-фолбэк локализованного времени: сообщение не
    ломается и ничего не теряет.

    Тег `<tg-time>` здесь сознательно НЕ используется: Telegram принимает
    его только в обратном направлении (`Message.text_html`), а при
    отправке в parse_mode=HTML он не документирован и ломает разметку.
    """
    if unix_time is None:
        return None
    try:
        moment = (
            unix_time.replace(tzinfo=APP_TZ)
            if unix_time.tzinfo is None
            else unix_time.astimezone(APP_TZ)
        )
    except (OverflowError, ValueError, OSError):
        return None
    fmt = (date_time_format or _DATE_TIME_DEFAULT_FORMAT).strip()
    if not fmt or any(char not in _DATE_TIME_FLAGS for char in fmt):
        return moment.isoformat(sep=" ", timespec="minutes")
    if "r" in fmt:
        return _relative_date_time(moment, now)
    parts: list[str] = []
    if "w" in fmt:
        parts.append(_WEEKDAYS_RU[moment.weekday()])
    if "d" in fmt:
        parts.append(f"{moment.day:02d}.{moment.month:02d}.{moment.year}")
    elif "D" in fmt:
        parts.append(f"{moment.day} {_MONTHS_RU[moment.month - 1]} {moment.year}")
    if "t" in fmt:
        parts.append(f"{moment.hour:02d}:{moment.minute:02d}")
    elif "T" in fmt:
        parts.append(f"{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}")
    if not parts:
        return moment.isoformat(sep=" ", timespec="minutes")
    return ", ".join(parts)


def _relative_date_time(moment: datetime, now: datetime | None) -> str:
    """Относительная формулировка (`r`): «сегодня в 19:00», «через 2 дня»."""
    current = now.astimezone(APP_TZ) if now is not None else datetime.now(APP_TZ)
    delta = moment - current
    seconds = int(delta.total_seconds())
    day_shift = (moment.date() - current.date()).days
    if day_shift == 0:
        return f"сегодня в {moment.hour:02d}:{moment.minute:02d}"
    if day_shift == 1:
        return f"завтра в {moment.hour:02d}:{moment.minute:02d}"
    if day_shift == -1:
        return f"вчера в {moment.hour:02d}:{moment.minute:02d}"
    if abs(seconds) < 60:
        return "сейчас" if seconds >= 0 else "минуту назад"
    minutes = abs(seconds) // 60
    if minutes < 60:
        return f"через {minutes} мин" if seconds > 0 else f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"через {hours} ч" if seconds > 0 else f"{hours} ч назад"
    days = abs(day_shift)
    return f"через {days} дн" if day_shift > 0 else f"{days} дн назад"


# —— Allow-list HTML значения (U1b, defense-in-depth) ————————————————————

# Разрешённая разметка Telegram. Всё остальное из БД (старые/подделанные
# строки, ручная правка SQLite) считается обычным текстом и экранируется.
_ALLOWED_TAGS = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "code",
        "pre",
        "blockquote",
        "a",
        "tg-spoiler",
        "span",
        "tg-emoji",
        "tg-time",
    }
)

# Атрибуты по тегам: остальные молча отбрасываются (onclick, style и пр.).
_ALLOWED_ATTRIBUTES = {
    "a": frozenset({"href"}),
    "code": frozenset({"class"}),
    "span": frozenset({"class"}),
    "blockquote": frozenset({"expandable"}),
    "pre": frozenset({"expandable"}),
    "tg-emoji": frozenset({"emoji-id"}),
    # `<tg-time unix="…" format="…">` — как его сериализует PTB; без
    # атрибутов тег остаётся безопасным текстом-обёрткой.
    "tg-time": frozenset({"unix", "format"}),
}
# Булевы атрибуты Telegram: достаточно самого факта наличия.
_FLAG_ATTRIBUTES = frozenset({"expandable"})
# Разрешённые class: спойлер и язык блока кода (`language-python`).
_LANGUAGE_CLASS_RE = re.compile(r"language-[A-Za-z0-9_+#-]{1,32}")
_ALLOWED_HREF_PREFIXES = ("http://", "https://", "tg://")
# Значения атрибутов `<tg-time>`: unix — целое неотрицательное (до 20 цифр,
# чтобы не принимать мусор), format — только документированные флаги Bot API.
_UNIX_ATTRIBUTE_RE = re.compile(r"[0-9]{1,20}")
_TIME_FORMAT_RE = re.compile(r"[rwdDtT]{1,16}")

_OPEN_TAG_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9-]*)([^<>]*)>")
_CLOSE_TAG_RE = re.compile(r"<\s*/\s*([a-zA-Z][a-zA-Z0-9-]*)\s*>")
_ATTR_RE = re.compile(r"""([a-zA-Z-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
# Строгий вид открывающего тега: имя без пробела после '<', затем только
# пары `ключ="значение"`, `ключ='значение'` или булев флаг. Текст вида
# «a <b & c> d» тегом не считается: иначе часть строки молча пропала бы.
_TAG_TAIL_RE = re.compile(r"""(?:\s+[a-zA-Z-]+(?:\s*=\s*(?:"[^"]*"|'[^']*'))?)*\s*/?""")
_ENTITY_RE = re.compile(r"&(?:amp|lt|gt|quot|apos|#[0-9]{1,7}|#x[0-9a-fA-F]{1,6});")


def sanitize_html(value: object) -> SafeHtml:
    """
    Проверяет HTML значения по allow-list и возвращает безопасную строку.

    Разрешены только теги/атрибуты Telegram; опасные теги и атрибуты
    вырезаются, их содержимое остаётся текстом. Значения атрибутов
    дополнительно проверяются: `href` — только http(s)/tg, `class` — только
    служебный, `emoji-id` — цифры. Символы `<`, `>`, `&` вне разметки
    экранируются, поэтому вызов идемпотентен: повторная проверка уже
    проверенного HTML ничего не меняет. Возвращается `SafeHtml`, чтобы
    компилятор шаблона не экранировал разметку второй раз.
    """
    raw = str(value if value is not None else "")
    out: list[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == "<":
            close = _CLOSE_TAG_RE.match(raw, index)
            if close is not None:
                name = close.group(1).lower()
                if name in _ALLOWED_TAGS:
                    out.append(f"</{name}>")
                index = close.end()
                continue
            opening = _OPEN_TAG_RE.match(raw, index)
            if opening is None or not _TAG_TAIL_RE.fullmatch(opening.group(2)):
                # Одиночный '<' — обычный текст, а не разметка.
                out.append("&lt;")
                index += 1
                continue
            name = opening.group(1).lower()
            if name in _ALLOWED_TAGS:
                attributes = _safe_attributes(name, opening.group(2))
                out.append(f"<{name}{attributes}>")
            # Неразрешённый тег выброшен, его текст остаётся содержимым.
            index = opening.end()
            continue
        if char == "&":
            entity = _ENTITY_RE.match(raw, index)
            if entity is not None:
                out.append(entity.group(0))
                index = entity.end()
            else:
                out.append("&amp;")
                index += 1
            continue
        if char == ">":
            out.append("&gt;")
            index += 1
            continue
        out.append(char)
        index += 1
    return SafeHtml("".join(out))


def _attribute_sort_key(name: str) -> tuple[int, str]:
    """
    Порядок атрибутов как у PTB: `unix` раньше `format` в `<tg-time>`.

    Раньше атрибуты сортировались по алфавиту (`format` раньше `unix`),
    поэтому санитизированный `<tg-time>` не совпадал с сериализацией PTB
    байт-в-байт, и повторная проверка давала другой текст. Порядок из
    `Message.text_html` (`unix`, затем `format`) сохраняется.
    """
    return (0, name) if name == "unix" else (1, name)


def _safe_attributes(name: str, raw: str) -> str:
    """Собирает разрешённые атрибуты тега; прочие отбрасываются."""
    allowed = _ALLOWED_ATTRIBUTES.get(name)
    if not allowed or not raw:
        return ""
    found = {
        match.group(1).lower(): (
            match.group(2) if match.group(2) is not None else match.group(3)
        )
        for match in _ATTR_RE.finditer(raw)
    }
    parts: list[str] = []
    for key in sorted(set(found) | _bare_flags(name, raw), key=_attribute_sort_key):
        if key not in allowed:
            continue
        value = _unescape(found.get(key) or "")
        if key in _FLAG_ATTRIBUTES:
            parts.append(f" {key}")
        elif key == "href":
            if not value.lower().startswith(_ALLOWED_HREF_PREFIXES):
                continue
            parts.append(f' href="{escape_html(value)}"')
        elif key == "class":
            allowed_classes = [
                item
                for item in value.split()
                if item == "tg-spoiler" or _LANGUAGE_CLASS_RE.fullmatch(item)
            ]
            if allowed_classes:
                parts.append(f' class="{escape_html(" ".join(allowed_classes))}"')
        elif key == "emoji-id":
            if not value.isdigit():
                continue
            parts.append(f' emoji-id="{value}"')
        elif key == "unix":
            if not _UNIX_ATTRIBUTE_RE.fullmatch(value):
                continue
            parts.append(f' unix="{value}"')
        elif key == "format":
            if not _TIME_FORMAT_RE.fullmatch(value):
                continue
            parts.append(f' format="{value}"')
    return "".join(parts)


def _bare_flags(name: str, raw: str) -> set[str]:
    """
    Булевы атрибуты без значения (`<blockquote expandable>`).

    Telegram документирует их именно в таком виде, а `_ATTR_RE` ищет только
    `key="value"`; без этой проверки повторная валидация теряла бы флаг.
    """
    allowed = _ALLOWED_ATTRIBUTES.get(name) or frozenset()
    return {
        token.lower()
        for token in re.findall(r"(?<![=\w-])([a-zA-Z-]+)(?=[\s/>]|$)", raw)
        if token.lower() in _FLAG_ATTRIBUTES and token.lower() in allowed
    }


# —— entities → грамматика шаблонов Stage 24 (Poster 2.4.1) ———————————————

# Маркеры грамматики для сущностей форматирования Telegram.
_ENTITY_MARKERS = {
    "bold": "**",
    "italic": "*",
    "underline": "__",
    "strikethrough": "~~",
    "spoiler": "||",
    "code": "`",
    "pre": "```",
}

# Типы-ссылки: при равных диапазонах они обрабатываются последними, чтобы
# вложенное форматирование оказалось внутри ссылки, а не наоборот.
_LINK_ENTITY_TYPES = frozenset({MessageEntity.TEXT_LINK, MessageEntity.URL})


# Литеральные спецсимволы грамматики экранируются обратным слэшем; скобки —
# удвоением, потому что `\[`/`\]` в ESCAPE_MAP нет (для них есть `[[`/`]]`).
_TEMPLATE_ESCAPES = {
    "\\": "\\\\",
    "*": "\\*",
    "_": "\\_",
    "~": "\\~",
    "|": "\\|",
    "`": "\\`",
    "(": "\\(",
    ")": "\\)",
    "{": "\\{",
    "}": "\\}",
    "[": "[[",
    "]": "]]",
}

# Токен кирпичика: код без квадратных и круглых скобок — ровно правила
# `template_parser.BRICK_PATTERN`. Здесь шаблон повторён, а не импортирован:
# `utils` не зависит от прикладных модулей. Согласованность с парсером
# проверяет тест `test_poster2_241_template_markup`.
_BRICK_TOKEN_RE = re.compile(r"\[[^\[\]\(\)]+\]")


def _brick_token_at(text: str, offset: int) -> str | None:
    """
    Валидный токен кирпичика `[код]`, начинающийся в `offset`, либо None.

    Не токен: `[[` слева и `]]` справа (это уже авторское экранирование
    литеральных скобок), а также содержимое с квадратными/круглыми скобками.
    Одиночная `]` сразу после токена допустима: парсер читает `[main]]` как
    кирпичик и литеральную `]`, а `]]` — как экранированную скобку.
    """
    if offset >= len(text) or text[offset] != "[":
        return None
    if offset > 0 and text[offset - 1] == "[":
        return None
    match = _BRICK_TOKEN_RE.match(text, offset)
    if match is None:
        return None
    end = match.end()
    # `]]` справа — авторское экранирование литеральной скобки. Одиночная
    # `]` после токена — литерал, как в грамматике парсера (`[a]]`), но
    # только если токен не вложен в незакрытую литеральную `[`: в `[a[b]]`
    # `[b]` — часть экранируемой скобочной конструкции, а не кирпичик.
    nested = text.count("[", 0, offset) > text.count("]", 0, offset)
    if text[end : end + 2] == "]]" or (text[end : end + 1] == "]" and nested):
        return None
    return match.group(0)


def _contains_brick_token(text: str) -> bool:
    """Есть ли в тексте хотя бы один валидный токен кирпичика."""
    return any(
        _brick_token_at(text, offset) is not None
        for offset, char in enumerate(text)
        if char == "["
    )


def _escape_template_text(text: str, *, keep_bricks: bool = True) -> str:
    """
    Экранирует литеральный текст секции, чтобы он не стал оформлением.

    Квадратные скобки экранируются удвоением, НО валидный кирпичик `[код]`
    остаётся собой: сообщение с entities — это правка авторского текста, и
    `[main]` в нём обязан остаться привязкой, а не превратиться в литерал
    `[[main]]`. Поэтому одиночная `[` с кодом и закрывающей `]` не
    экранируется, а всё остальное (в том числе пользовательские `[[`/`]]`,
    которые уже означают литеральную скобку) — экранируется как раньше.

    `keep_bricks=False` — для содержимого code/pre: буквальный спан не
    содержит кирпичиков, и `[main]` в нём остаётся текстом.
    """
    out: list[str] = []
    offset = 0
    length = len(text)
    while offset < length:
        if keep_bricks:
            brick = _brick_token_at(text, offset)
            if brick is not None:
                out.append(brick)
                offset += len(brick)
                continue
        out.append(_TEMPLATE_ESCAPES.get(text[offset], text[offset]))
        offset += 1
    return "".join(out)


def entities_to_template_markup(
    text: str, entities: Sequence[MessageEntity] | None
) -> str:
    """
    Текст сообщения с entities → канонический текст грамматики Stage 24.

    Результат — не HTML, а авторская запись шаблона: `**жирный**`,
    `*курсив*`, `__подчёркнутый__`, `~~зачёркнутый~~`, `||спойлер||`,
    `` `код` ``, ```` ```блок``` ````, `подпись(URL)` для `text_link`.
    Так форматирование из клиента Telegram переживает сохранение секции,
    повторное открытие редактора и рендер анонса.

    Правила:

    - смещения entities — в 16-битных код-юнитах UTF-16, разбор идёт по
      utf-16-байтам (как `entities_to_html`), поэтому кириллица и эмодзи
      не сдвигают границы;
    - вложенность entities сохраняется: `**` + text_link на том же диапазоне
      дают `**{подпись}(URL)**` (ссылка — внутри жирного), а не наоборот;
    - `text_link` пишется как `подпись(URL)`, даже если URL нет в видимом
      тексте; схемы `tg://` сохраняются;
    - при равных смещениях entities порядок не важен: сортировка делает
      внешним слоем более длинный диапазон, а ссылку — внутренним;
    - `url` остаётся текстом: Bot API сам делает голый URL кликабельным;
    - `text_mention`, `custom_emoji`, `date_time`, `mention`, `hashtag` и
      прочие неподдерживаемые грамматикой типы деградируют в видимый текст
      (оформление теряется, содержимое — нет);
    - `pre` с языком теряет язык: в грамматике его нет;
    - code/pre — буквальные спаны: содержимое не экранируется, но если в
      нём есть обратная кавычка, спан невозможен — остаётся обычный текст;
    - если entities в сообщении ЕСТЬ, все литеральные `*`, `_`, `~`, `` ` ``,
      `|`, `(`, `)`, `{`, `}` обычного текста экранируются; квадратные скобки
      тоже, КРОМЕ валидного токена кирпичика `[код]` — он остаётся
      кирпичиком (`**Итог**: [main]`), иначе правка шаблона сообщением с
      entities уничтожала бы привязки;
    - деградация text_link: `http(s)`, `tg://`, `t.me/…` и домены без схемы
      записываются ссылкой; URL со пробелами, скобками `(`/`)`,
      квадратными скобками или обратным слэшем, а также схемы вне
      грамматики (`mailto:`, `ftp:` и пр.) превращаются в подпись БЕЗ
      ссылки — предсказуемо и без ошибки сохранения (грамматика не умеет их
      записать);
    - если entities НЕТ, текст возвращается без изменений: прежнее
      поведение редактора, где `**` пользователь набирал сам.
    """
    if not text or not entities:
        return text

    utf16_text = text.encode("utf-16-le", "surrogatepass")
    total_units = len(utf16_text) // 2
    sliced: dict[MessageEntity, str] = {}
    for entity in entities:
        if entity.length <= 0 or entity.offset < 0:
            continue
        if entity.offset + entity.length > total_units:
            continue
        start = entity.offset * 2
        sliced[entity] = utf16_text[start : start + entity.length * 2].decode(
            "utf-16-le"
        )
    if not sliced:
        return text
    return _parse_template_markup(text, sliced)


def _template_entity_sort_key(
    item: tuple[MessageEntity, str],
) -> tuple[int, int, int]:
    """
    Порядок разбора entities: внешний слой первым.

    Сортировка по смещению, затем по концу диапазона в обратном порядке:
    при одинаковом начале более длинная entity содержит короткую и обязана
    стать внешним слоем, иначе короткая «съест» её хвост. При полном
    совпадении диапазонов форматирование идёт раньше ссылки: ссылка
    оказывается внутри (`**{подпись}(URL)**`), а не наоборот.
    """
    entity = item[0]
    link_layer = 1 if entity.type in _LINK_ENTITY_TYPES else 0
    return (entity.offset, -(entity.offset + entity.length), link_layer)


def _parse_template_markup(
    message_text: str,
    entities: dict[MessageEntity, str],
    *,
    offset: int = 0,
    top_level: bool = True,
) -> str:
    """
    Рекурсивная сборка авторской записи: вложенность entities → маркеры.

    Алгоритм повторяет `_parse_html` (срезы по UTF-16, сортировка по
    смещению, вложенные entities разбираются отдельным вызовом), но вместо
    HTML-тегов пишет маркеры грамматики Stage 24. Порядок при равных
    смещениях задаёт `_template_entity_sort_key`: внешний слой — более
    длинный диапазон, ссылка внутри форматирования на том же диапазоне.
    """
    utf16_text = message_text.encode("utf-16-le", "surrogatepass")
    markup = ""
    last_offset = 0
    sorted_entities = sorted(entities.items(), key=_template_entity_sort_key)
    parsed: list[MessageEntity] = []

    for entity, entity_text in sorted_entities:
        if entity in parsed:
            continue
        if entity.offset - offset < last_offset:
            # Перекрывающиеся, но не вложенные entities: не ломаем уже
            # выданный фрагмент, содержимое остаётся текстом.
            continue
        nested = {
            nested_entity: nested_text
            for nested_entity, nested_text in sorted_entities
            if nested_entity.offset >= entity.offset
            and nested_entity.offset + nested_entity.length
            <= entity.offset + entity.length
            and nested_entity != entity
        }
        parsed.extend(nested.keys())

        # Текст между предыдущей entities и текущей экранируется; кирпичики
        # `[код]` в нём сохраняются, иначе правка шаблона сообщением с
        # entities уничтожала бы привязки.
        gap = _escape_template_text(
            utf16_text[last_offset * 2 : (entity.offset - offset) * 2].decode(
                "utf-16-le"
            )
        )
        # Обычная запись `подпись(URL)` верна только когда подпись — весь
        # хвост секции перед скобкой, внутри неё нет разметки, а сама
        # подпись не содержит кирпичика: ссылка с кирпичиком в подписи
        # читается только в явной форме `{подпись}(URL)`.
        plain_label = (
            top_level
            and offset == 0
            and entity.offset == 0
            and not markup
            and not gap
            and not nested
            and not _contains_brick_token(entity_text)
            and not entity_text.endswith((" ", "\n", "\t"))
        )

        kind = entity.type
        if kind in {MessageEntity.CODE, MessageEntity.PRE}:
            # Буквальный спан: содержимое не разбирается и не экранируется.
            # Обратная кавычка внутри сделала бы спан неоднозначным — тогда
            # деградируем в обычный экранированный текст.
            fence = _ENTITY_MARKERS[kind]
            if fence in entity_text:
                wrapped = _escape_template_text(entity_text, keep_bricks=False)
            else:
                wrapped = f"{fence}{entity_text}{fence}"
        elif nested:
            inner = _parse_template_markup(
                entity_text, nested, offset=entity.offset, top_level=False
            )
            wrapped = _wrap_markup(entity, inner, plain_label=plain_label)
        else:
            inner = _escape_template_text(entity_text)
            wrapped = _wrap_markup(entity, inner, plain_label=plain_label)

        markup += gap
        markup += wrapped
        last_offset = entity.offset - offset + entity.length

    markup += _escape_template_text(utf16_text[last_offset * 2 :].decode("utf-16-le"))
    return markup


def _wrap_markup(entity: MessageEntity, inner: str, *, plain_label: bool) -> str:
    """
    Маркер грамматики для entity; неподдерживаемые типы дают просто текст.

    `text_link` пишется как `подпись(URL)` — так устроена грамматика Stage 24
    (`ТЕКСТ(URL)`). Такая запись годится, только когда подпись — хвост текста
    перед скобкой и внутри неё нет вложенной разметки; иначе используется
    явная форма `{подпись}(URL)`, где грамматика читает подпись целиком.
    Адрес берётся из entity, даже если его нет в тексте.

    Неподдерживаемые грамматикой типы (`text_mention`, `custom_emoji`,
    `date_time`, `url`, `mention` и пр.) остаются видимым текстом: содержимое
    не теряется.
    """
    kind = entity.type
    marker = _ENTITY_MARKERS.get(kind)
    if marker is not None:
        return f"{marker}{inner}{marker}"
    if kind == MessageEntity.TEXT_LINK and entity.url:
        url = str(entity.url).strip()
        if _link_is_expressible(url):
            return f"{inner}({url})" if plain_label else f"{{{inner}}}({url})"
        return inner
    return inner


def _link_is_expressible(url: str) -> bool:
    """
    Можно ли записать URL внутри `( )` без искажения.

    Запись ссылки — `подпись(URL)` (или `{подпись}(URL)`): закрывающая
    скобка обрывает адрес, `[`/`]` грамматика читает как кирпичик или как
    обрывочную скобку (`TemplateParseError`), а `\\` — как экранирование
    следующего символа. Поэтому такой text_link деградирует в подпись без
    ссылки — предсказуемо и без ошибки сохранения. Схемы, которые
    грамматика не представляет (`mailto:`, `ftp:` и любые не
    http/https/tg), деградируют так же.
    """
    if not url or any(char.isspace() for char in url):
        return False
    if any(char in url for char in "()[]\\"):
        return False
    lowered = url.lower()
    return lowered.startswith(
        ("http://", "https://", "tg://", "t.me/", "www.")
    ) or bool(re.match(r"^[a-z0-9\-]+(\.[a-z0-9\-]+)+", lowered))


# —— Ссылки внутри ссылок ——————————————————————————————————————————————

_LINK_TAG_RE = re.compile(r"</?a(?:\s[^<>]*)?>")


def strip_link_tags(html: str) -> str:
    """
    Убирает теги `<a …>`/`</a>`, оставляя их содержимое (U1b-fix).

    Нужно для подписи ссылки шаблона: значение с URL-entity уже содержит
    `<a>`, и обёртка `[{{код}}](url)` давала вложенные ссылки — Telegram
    отклоняет такое сообщение. Остальные теги (`<b>`, `<code>`, …) и
    сущности (`&amp;`) не трогаются: меняется только вложенность ссылок.
    """
    return _LINK_TAG_RE.sub("", str(html))


# —— Разбиение длинного HTML на части ————————————————————————————————————

# Максимум сущности-ссылки вида "&...;" (наши экранировки не длиннее 7):
# дальше точку с запятой считаем обычным текстом, чтобы не «съедать»
# половины абзаца при неэкранированном '&'.
_MAX_ENTITY_SPAN = 12


def split_html(text: str, limit: int) -> list[str]:
    """
    Разбивает HTML на части длиной не более limit (код-юнитов UTF-16).

    Гарантии:
    — теги и ссылки-сущности (&...;) не разрываются никогда: разрез идёт
      только между ними и внутри обычного текста (эмодзи не делятся);
    — теги, открытые на границе, закрываются в конце части и переоткрываются
      в начале следующей — каждая часть остаётся валидным HTML;
    — видимый текст не теряется и не дублируется;
    — граница предпочитается сразу после '\\n'; если переводов строки нет,
      части наполняются до предела по символам.

    Лимит считается по «сырому» HTML: он не короче видимого текста, поэтому
    части укладываются в лимит и после разбора Telegram. Редкий случай
    «один токен больше лимита» (гипердлинный атрибут) не лечится — такой
    токен уходит целиком.
    """
    if limit <= 0:
        raise ValueError("Лимит части должен быть положительным")
    if utf16_len(text) <= limit:
        return [text]

    tokens = _tokenize(text)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    # Открытые теги: (имя, полный открывающий тег) — полный тег нужен,
    # чтобы переоткрыть тег в следующей части вместе с атрибутами
    stack: list[tuple[str, str]] = []
    # Удобные границы разреза после '\\n': (позиция в current,
    # длина по этой позиции, стек открытых тегов в этот момент)
    marks: list[tuple[int, int, list[tuple[str, str]]]] = []

    def fits(token: str, new_stack: list[tuple[str, str]]) -> bool:
        return current_len + utf16_len(token) + _closes_len(new_stack) <= limit

    def flush() -> bool:
        """
        Выгружает заполненную часть, предпочитая границу после '\\n'.

        Возвращает True при срезе по границе (содержимое частично остаётся
        в новой части) и False при полной выгрузке. Стек не переносится и не
        сбрасывается: он описывает состояние ВХОДА на точке разреза, а
        переоткрытые теги новой части воспроизводят состояние границы.
        """
        nonlocal current, current_len, marks
        for position, length_at, snapshot in reversed(marks):
            if position <= 0:
                continue
            reopened = [tag for _, tag in snapshot]
            reopened_len = sum(utf16_len(tag) for tag in reopened)
            rest_len = current_len - length_at
            # Часть 1 (префикс + закрытие тегов, открытых на границе)
            # влезает всегда — инвариант выполнялся в момент появления
            # границы. Часть 2 должна уложиться вместе с закрытиями стека.
            if reopened_len + rest_len + _closes_len(stack) <= limit:
                chunks.append("".join(current[:position]) + _closes(snapshot))
                current = reopened + current[position:]
                current_len = reopened_len + rest_len
                marks = [
                    (
                        len(reopened) + (pos - position),
                        reopened_len + (ln - length_at),
                        snap,
                    )
                    for (pos, ln, snap) in marks
                    if pos > position
                ]
                return True
        # Свободной границы нет: выгружаем всё, а открытые теги переоткрываем
        # в новой части — во входе впереди ещё есть их закрывающие теги
        chunks.append("".join(current) + _closes(stack))
        reopened = [tag for _, tag in stack]
        current = reopened
        current_len = sum(utf16_len(tag) for tag in reopened)
        marks = []
        return False

    for token in tokens:
        while current and not fits(token, _after(stack, token)):
            if not flush():
                # Всё выгружено (с переоткрытием): токен встаёт в новую
                # часть — повторная выгрузка дала бы пустые теги-пары
                break
        stack = _after(stack, token)
        current.append(token)
        current_len += utf16_len(token)
        if token == "\n":
            marks.append((len(current), current_len, list(stack)))

    if current:
        chunks.append("".join(current) + _closes(stack))
    return chunks


def _tokenize(text: str) -> list[str]:
    """
    Разбивает HTML на токены: теги, ссылки-сущности (&...;) и одиночные
    символы. Токен не разрывается при сплите никогда.
    """
    tokens: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "<":
            end = _scan_tag(text, index)
            if end is not None:
                tokens.append(text[index:end])
                index = end
                continue
            # Одиночный '<' (в нашем HTML не встречается — текст экранирован)
            tokens.append(char)
            index += 1
        elif char == "&":
            semi = text.find(";", index + 1)
            if semi != -1 and semi - index <= _MAX_ENTITY_SPAN:
                tokens.append(text[index : semi + 1])
                index = semi + 1
            else:
                tokens.append(char)
                index += 1
        else:
            tokens.append(char)
            index += 1
    return tokens


def _scan_tag(text: str, start: int) -> int | None:
    """Индекс символа после '>', если тег корректен (кавычки атрибутов учтены)."""
    quote = ""
    index = start + 1
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == ">":
            return index + 1
        index += 1
    return None


def _after(stack: list[tuple[str, str]], token: str) -> list[tuple[str, str]]:
    """Стек открытых тегов после применения токена (текст тег не меняет)."""
    if not (token.startswith("<") and token.endswith(">")):
        return stack
    if token.startswith("</"):
        return _pop(stack, token[2:-1].strip())
    if token.startswith("<!") or token.endswith("/>"):
        return stack
    inner = token[1:-1].strip()
    if not inner:
        return stack
    return stack + [(inner.split()[0], token)]


def _pop(stack: list[tuple[str, str]], name: str) -> list[tuple[str, str]]:
    """Закрывает тег с данным именем (и всё, что в нём осталось открытым)."""
    for index in range(len(stack) - 1, -1, -1):
        if stack[index][0] == name:
            return stack[:index]
    return stack  # лишний закрывающий тег — игнорируем


def _closes(stack: list[tuple[str, str]]) -> str:
    """Закрывающие теги стека в порядке закрытия."""
    return "".join(f"</{name}>" for name, _ in reversed(stack))


def _closes_len(stack: list[tuple[str, str]]) -> int:
    """Длина _closes в код-юнитах UTF-16."""
    return sum(utf16_len(f"</{name}>") for name, _ in reversed(stack))
