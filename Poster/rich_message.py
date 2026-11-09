# rich_message.py
"""
Изолированный адаптер Bot API для Rich Message (Poster 2.5, этап E).

Telegram Bot API 10.1 ввёл самостоятельный тип структурированного сообщения
(`sendRichMessage`, §46 спецификации), а python-telegram-bot 22.8 его ещё
не знает: ни метода, ни классов `InputRichMessage`/`InputRichBlock*`.
Поэтому здесь маленький адаптер, а не вторая библиотека Telegram (§50):

- типы данных (`InputRichMessage`, `InputRichBlock*`) и их проверка;
- один сетевой вызов через ПУБЛИЧНЫЙ PTB-механизм произвольных эндпоинтов
  `Bot.do_api_request` (появился в PTB 20.8 именно для методов, которых
  библиотека ещё не поддерживает). Токен не дублируется и не читается:
  его использует штатный клиент бота, поэтому он не может попасть в наши
  логи или текст ошибки;
- таймауты, повторные попытки и разбор `ok=false` — штатные средства PTB
  (`telegram.request`), ошибки приходят готовыми `telegram.error.*`.

Схема сериализации сверена с официальной документацией Telegram Bot API
10.1 (core.telegram.org/bots/api, разделы InputRichMessage /
InputRichBlock):

- `heading` — поля `text` и обязательный `size` (1–6, 1 — крупнейший);
- `paragraph` — `text`;
- `list` — `items` из объектов `InputRichBlockListItem`, у каждого свой
  `blocks` (элемент списка — не блок, в `RichBlock` не входит);
- `divider` — без полей;
- `details` — `summary`, `blocks` и необязательный `is_open`: поле
  передаётся ТОЛЬКО при True, отсутствие означает «свёрнут».

Bot API 10.4 добавил блоки §15.2 (проверено по официальной схеме):

- `table` — `cells` (массив строк, каждая — массив ячеек) и необязательные
  `is_bordered`/`is_striped`/`is_compact`/`caption`; ячейка
  (`InputRichBlockTableCell`) — `text`, `is_header`, `colspan`, `rowspan` и
  ОБЯЗАТЕЛЬНЫЕ `align`/`valign`;
- `blockquote` — `blocks` и необязательный `credit` (НЕ `pullquote` и НЕ
  `expandable_blockquote`: это разные сущности);
- `slideshow` — `blocks` из фото-блоков (`photo`: `InputMediaPhoto`,
  необязательная подпись `RichBlockCaption`) и необязательный `caption`.

Поле `text` у `heading`/`paragraph`, `summary` у `details`, `credit` у
цитаты и `caption` у таблицы по официальной схеме имеет тип `RichText`:
строка простого текста, массив таких же значений либо типизированный
объект. Poster использует два случая: строку и `RichTextUrl` (кликабельная
подпись вместо «голого» адреса, §7 2.5.4).

Загрузка файлов слайд-шоу. `InputFile` в `photo.media` нельзя оставить
внутри `rich_message`: PTB `RequestParameter.from_input` разбирает только
ключи ВЕРХНЕГО уровня `api_kwargs`, а вложенный словарь просто отдаёт
`json.dumps`, который падает на `InputFile`. Поэтому `send_rich_message`
«хоистит» файлы: находит их в сериализованном payload, поднимает на
верхний уровень (`rich_media_0`, …) и заменяет `media` на
`attach://rich_media_N` — ровно та форма, которую PTB превращает в
multipart-загрузку.

Rich Message используется только для служебных экранов «Помощь» и
«О проекте» (§46): анонсы отправляются обычными сообщениями/альбомами.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, NoReturn

from telegram import InputFile, InputMediaPhoto, Message
from telegram.error import (
    BadRequest,
    Conflict,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
)

logger = logging.getLogger(__name__)

# Имя метода Bot API. Отдельная константа: единая точка правки и удобный
# поиск места, где приложение вообще ходит мимо PTB.
SEND_RICH_MESSAGE_ENDPOINT = "sendRichMessage"

# Максимальная вложенность раскрываемых блоков. Справке хватает одного
# уровня, ограничение защищает от случайной рекурсивной структуры и от
# бесконечного дерева, которое сервер всё равно отвергнет.
MAX_DETAILS_DEPTH = 3


class RichMessageError(ValueError):
    """Структура Rich Message не соответствует схеме (ошибка до сети)."""


# —— блоки InputRichBlock ————————————————————————————————————————————
#
# Каждый блок умеет отдать себя словарём Bot API (`to_dict`). Классы
# неизменяемые: отправленная структура не должна меняться задним числом.


@dataclass(frozen=True)
class InputRichTextUrl:
    """
    Ссылка с подписью (`RichTextUrl`, тип `url`).

    По официальной схеме Bot API 10.1 это `RichText`: `text` — сама подпись
    (снова `RichText`), `url` — адрес. Обычная строка со ссылкой тоже
    допустима, но Telegram покажет её как автодетект адреса; здесь нужна
    короткая кликабельная подпись («Открыть на GitHub»), поэтому тип
    передаётся явно.
    """

    text: RichText
    url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "url",
            "text": rich_text_to_dict(self.text),
            "url": self.url,
        }


@dataclass(frozen=True)
class InputRichTextBold:
    """
    Жирный фрагмент текста (`RichTextBold`, тип `bold`).

    Утверждённые тексты «Помощи» и «О проекте» выделяют жирным отдельные
    фразы внутри абзаца (финальное задание 2026-11-08 §13.7, §14.6). По
    официальной схеме Bot API `text` — снова `RichText`, но здесь нужен
    только простой текст: вложенные выделения не используются.
    """

    text: RichText

    def to_dict(self) -> dict[str, Any]:
        return {"type": "bold", "text": rich_text_to_dict(self.text)}


# Официальный `RichText`: строка, типизированный объект или их массив.
# Poster использует один уровень вложенности (подпись ссылки и жирная фраза
# внутри абзаца — строка): этого достаточно, рекурсивный разбор не нужен.
RichText = (
    str
    | InputRichTextUrl
    | InputRichTextBold
    | Sequence[str | InputRichTextUrl | InputRichTextBold]
)


def rich_text_to_dict(value: RichText) -> Any:
    """Сериализует `RichText` в форму Bot API (строка остаётся строкой)."""
    if isinstance(value, (InputRichTextUrl, InputRichTextBold)):
        return value.to_dict()
    if isinstance(value, str):
        return value
    return [rich_text_to_dict(item) for item in value]


def rich_text_plain(value: RichText) -> str:
    """Плоский текст `RichText` (для проверок и логов, не для отправки)."""
    if isinstance(value, (InputRichTextUrl, InputRichTextBold)):
        return rich_text_plain(value.text)
    if isinstance(value, str):
        return value
    return "".join(rich_text_plain(item) for item in value)


@dataclass(frozen=True)
class InputRichBlockSectionHeading:
    """Заголовок раздела (`InputRichBlockSectionHeading`, `type: "heading"`)."""

    text: RichText
    # 1 — самый крупный заголовок, 6 — самый мелкий (официальная схема).
    size: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "heading",
            "text": rich_text_to_dict(self.text),
            "size": self.size,
        }


@dataclass(frozen=True)
class InputRichBlockParagraph:
    """Абзац текста (`InputRichBlockParagraph`, §49)."""

    text: RichText

    def to_dict(self) -> dict[str, Any]:
        return {"type": "paragraph", "text": rich_text_to_dict(self.text)}


@dataclass(frozen=True)
class InputRichBlockListItem:
    """
    Элемент списка (`InputRichBlockListItem`).

    Это НЕ блок: объект живёт только внутри `InputRichBlockList`, поэтому в
    `RichBlock`/`BLOCK_TYPES` не входит. `blocks` — содержимое элемента
    (например, один абзац), обязательное и непустое.
    """

    blocks: Sequence[RichBlock]

    def to_dict(self, *, depth: int = 0) -> dict[str, Any]:
        return {
            "blocks": [block_to_dict(block, depth=depth) for block in self.blocks],
        }


@dataclass(frozen=True)
class InputRichBlockList:
    """Список действий (`InputRichBlockList`, §49)."""

    items: Sequence[InputRichBlockListItem]

    def to_dict(self, *, depth: int = 0) -> dict[str, Any]:
        return {
            "type": "list",
            "items": [item.to_dict(depth=depth) for item in self.items],
        }


@dataclass(frozen=True)
class InputRichBlockDivider:
    """Разделитель крупных разделов (`InputRichBlockDivider`, §49)."""

    def to_dict(self) -> dict[str, Any]:
        return {"type": "divider"}


@dataclass(frozen=True)
class InputRichBlockDetails:
    """
    Раскрываемый блок (`InputRichBlockDetails`, §49).

    `is_open` — необязательный флаг «развернуть сразу»: справка показывает
    разделы свёрнутыми, поэтому поле не передаётся вовсе (отсутствие =
    свёрнуто). True передаётся как есть; False избыточно и не сериализуется.
    """

    summary: RichText
    blocks: Sequence[RichBlock] = field(default_factory=tuple)
    is_open: bool | None = None

    def to_dict(self, *, depth: int = 0) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "details",
            "summary": rich_text_to_dict(self.summary),
            "blocks": [block_to_dict(block, depth=depth + 1) for block in self.blocks],
        }
        if self.is_open:
            payload["is_open"] = True
        return payload


# —— таблица (§15.2, Bot API 10.4) ————————————————————————————————————

# Допустимые значения обязательных выравниваний ячейки (официальная схема).
TABLE_ALIGNMENTS: tuple[str, ...] = ("left", "center", "right")
TABLE_VALIGNMENTS: tuple[str, ...] = ("top", "middle", "bottom")

TableAlign = Literal["left", "center", "right"]
TableVAlign = Literal["top", "middle", "bottom"]


@dataclass(frozen=True)
class InputRichBlockTableCell:
    """
    Ячейка таблицы (`InputRichBlockTableCell`).

    `align` и `valign` по официальной схеме ОБЯЗАТЕЛЬНЫ: сервер не выбирает
    выравнивание за нас, а молчаливая подстановка «left/top» скрыла бы
    ошибку вёрстки. `text` необязателен (пустая ячейка), остальные поля —
    необязательные уточнения.
    """

    align: TableAlign
    valign: TableVAlign
    text: RichText | None = None
    is_header: bool | None = None
    colspan: int | None = None
    rowspan: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "align": self.align,
            "valign": self.valign,
        }
        if self.text is not None:
            payload["text"] = rich_text_to_dict(self.text)
        if self.is_header is not None:
            payload["is_header"] = self.is_header
        if self.colspan is not None:
            payload["colspan"] = self.colspan
        if self.rowspan is not None:
            payload["rowspan"] = self.rowspan
        return payload


@dataclass(frozen=True)
class InputRichBlockTable:
    """
    Таблица (`InputRichBlockTable`, `type: "table"`).

    `cells` — строки таблицы, каждая строка — непустой массив ячеек.
    Строки могут иметь разную длину (ячейки с `colspan`/`rowspan`), поэтому
    выравнивать их «по ширине» здесь нельзя.
    """

    cells: Sequence[Sequence[InputRichBlockTableCell]]
    is_bordered: bool | None = None
    is_striped: bool | None = None
    is_compact: bool | None = None
    caption: RichText | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "table",
            "cells": [[cell.to_dict() for cell in row] for row in self.cells],
        }
        if self.is_bordered is not None:
            payload["is_bordered"] = self.is_bordered
        if self.is_striped is not None:
            payload["is_striped"] = self.is_striped
        if self.is_compact is not None:
            payload["is_compact"] = self.is_compact
        if self.caption is not None:
            payload["caption"] = rich_text_to_dict(self.caption)
        return payload


# —— цитата (§15.2, Bot API 10.4) —————————————————————————————————————


@dataclass(frozen=True)
class InputRichBlockBlockQuotation:
    """
    Rich-цитата (`InputRichBlockBlockQuotation`, `type: "blockquote"`).

    Это НЕ `expandable_blockquote` и не `pullquote`: у цитаты есть настоящее
    содержимое (`blocks`, например абзацы и подпись автора) и необязательный
    `credit`. Раскрываемость задаётся не здесь, а обёрткой `details`.
    """

    blocks: Sequence[RichBlock] = field(default_factory=tuple)
    credit: RichText | None = None

    def to_dict(self, *, depth: int = 0) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "blockquote",
            "blocks": [block_to_dict(block, depth=depth) for block in self.blocks],
        }
        if self.credit is not None:
            payload["credit"] = rich_text_to_dict(self.credit)
        return payload


# —— слайд-шоу (§15.3, Bot API 10.4) ——————————————————————————————————


@dataclass(frozen=True)
class RichBlockCaption:
    """Подпись медиа (`RichBlockCaption`): `text` и необязательный `credit`."""

    text: RichText
    credit: RichText | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": rich_text_to_dict(self.text)}
        if self.credit is not None:
            payload["credit"] = rich_text_to_dict(self.credit)
        return payload


@dataclass(frozen=True)
class InputRichBlockPhoto:
    """
    Фото-блок слайд-шоу (`InputRichBlockPhoto`, `type: "photo"`).

    `photo` — штатный `telegram.InputMediaPhoto`: он умеет и `file_id`, и
    `InputFile` (путь/байты). Именно этот объект разбирает механизм загрузки
    файлов в `send_rich_message`.
    """

    photo: InputMediaPhoto
    caption: RichBlockCaption | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "photo",
            "photo": self.photo.to_dict(),
        }
        if self.caption is not None:
            payload["caption"] = self.caption.to_dict()
        return payload


@dataclass(frozen=True)
class InputRichBlockSlideshow:
    """
    Слайд-шоу (`InputRichBlockSlideshow`, `type: "slideshow"`).

    `blocks` — ТОЛЬКО фото-блоки, порядок задаёт порядок кадров. Требование
    §15.3: настоящий `<tg-slideshow>`, а не подделка кнопками.
    """

    blocks: Sequence[InputRichBlockPhoto] = field(default_factory=tuple)
    caption: RichBlockCaption | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "slideshow",
            "blocks": [block.to_dict() for block in self.blocks],
        }
        if self.caption is not None:
            payload["caption"] = self.caption.to_dict()
        return payload


RichBlock = (
    InputRichBlockSectionHeading
    | InputRichBlockParagraph
    | InputRichBlockList
    | InputRichBlockDivider
    | InputRichBlockDetails
    | InputRichBlockTable
    | InputRichBlockBlockQuotation
    | InputRichBlockPhoto
    | InputRichBlockSlideshow
)

# Все классы блоков — для проверки «это вообще известный блок».
BLOCK_TYPES: tuple[type, ...] = (
    InputRichBlockSectionHeading,
    InputRichBlockParagraph,
    InputRichBlockList,
    InputRichBlockDivider,
    InputRichBlockDetails,
    InputRichBlockTable,
    InputRichBlockBlockQuotation,
    InputRichBlockPhoto,
    InputRichBlockSlideshow,
)


# —— контейнер InputRichMessage ——————————————————————————————————————


@dataclass(frozen=True)
class InputRichMessage:
    """
    Содержимое `rich_message` (§48).

    Ровно один способ задаётся: `markdown`, `html` или `blocks`. Справка
    Poster использует `blocks` (§48) — явные разделы и раскрываемые блоки.
    """

    markdown: str | None = None
    html: str | None = None
    blocks: Sequence[RichBlock] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Словарь для Bot API; невалидная структура — `RichMessageError`."""
        validate_rich_message(self)
        if self.markdown is not None:
            return {"markdown": self.markdown}
        if self.html is not None:
            return {"html": self.html}
        assert self.blocks is not None  # проверено validate_rich_message
        return {"blocks": [block_to_dict(block) for block in self.blocks]}


def block_to_dict(block: RichBlock, depth: int = 0) -> dict[str, Any]:
    """Сериализует блок; для `details` рекурсия ограничена MAX_DETAILS_DEPTH."""
    if not isinstance(block, BLOCK_TYPES):
        raise RichMessageError(
            f"Неизвестный блок Rich Message: {type(block).__name__} "
            f"(ожидались {', '.join(cls.__name__ for cls in BLOCK_TYPES)})"
        )
    if isinstance(block, InputRichBlockDetails):
        if depth >= MAX_DETAILS_DEPTH:
            raise RichMessageError(
                "Слишком глубокая вложенность раскрываемых блоков "
                f"(максимум {MAX_DETAILS_DEPTH})."
            )
        return block.to_dict(depth=depth)
    if isinstance(block, InputRichBlockBlockQuotation):
        # Цитата тоже контейнер: её содержимое может снова содержать
        # `details`, поэтому глубину нельзя сбрасывать в ноль.
        return block.to_dict(depth=depth)
    return block.to_dict()


# —— проверка структуры (до сети) ————————————————————————————————————


def _require_text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RichMessageError(f"{what}: ожидается непустая строка.")
    return value


def _require_rich_text(value: Any, what: str) -> RichText:
    """
    Проверяет `RichText`: непустая строка, `RichTextUrl`, `RichTextBold` или
    их массив.

    Проверка ДО сети: тип `text`/`summary` по официальной схеме — `RichText`,
    поэтому строка и ссылка с подписью равноправны, а случайный `None` или
    число должны упасть здесь, а не отказом Telegram.
    """
    if isinstance(value, InputRichTextUrl):
        _require_rich_text(value.text, f"{what}.text")
        _require_url(value.url, f"{what}.url")
        return value
    if isinstance(value, InputRichTextBold):
        _require_rich_text(value.text, f"{what}.text")
        return value
    if isinstance(value, str):
        _require_text(value, what)
        return value
    if isinstance(value, (bytes, bytearray)) or not isinstance(value, Sequence):
        raise RichMessageError(
            f"{what}: ожидается строка, RichTextUrl, RichTextBold или их список."
        )
    if not value:
        raise RichMessageError(f"{what}: список RichText пуст.")
    for item in value:
        _require_rich_text(item, f"{what}[]")
    return value


def _require_url(value: Any, what: str) -> str:
    """Адрес ссылки: непустая строка со схемой http(s) (схема Bot API)."""
    text = _require_text(value, what)
    if not text.startswith(("http://", "https://")):
        raise RichMessageError(f"{what}: ожидается ссылка http(s)://…")
    return text


def validate_rich_message(message: InputRichMessage | Mapping[str, Any]) -> None:
    """
    Проверяет структуру `InputRichMessage` перед отправкой.

    Ошибки — `RichMessageError` (наследник `ValueError`) с понятным текстом
    на русском: они означают ошибку в коде, а не отказ Telegram, и до сети
    такое сообщение не доходит. Для готового словаря проверяется набор
    ключей и блоки; свободный `html`/`markdown` разбирает сервер.
    """
    if isinstance(message, Mapping):
        ways = [
            key
            for key in ("markdown", "html", "blocks")
            if message.get(key) is not None
        ]
        if len(ways) != 1:
            raise RichMessageError(
                "InputRichMessage: задайте ровно один способ — markdown, html "
                f"или blocks (задано: {', '.join(ways) or 'ничего'})."
            )
        if ways[0] in ("markdown", "html"):
            _require_text(message[ways[0]], f"InputRichMessage.{ways[0]}")
            return
        blocks = message["blocks"]
        if isinstance(blocks, (str, bytes)) or not isinstance(blocks, Sequence):
            raise RichMessageError("InputRichMessage.blocks: ожидается список блоков.")
        if not blocks:
            raise RichMessageError("InputRichMessage.blocks: список блоков пуст.")
        # Готовый словарь Bot API мог прийти от вызывающего кода/теста:
        # блоки в нём уже сериализованы, их внутренние поля проверяет
        # сервер. Структурную проверку проходит только наш dataclass.
        for block in blocks:
            if isinstance(block, BLOCK_TYPES):
                _validate_block(block, depth=0)
        return
    if not isinstance(message, InputRichMessage):
        raise RichMessageError(
            "sendRichMessage: ожидается InputRichMessage или словарь Bot API, "
            f"получено {type(message).__name__}."
        )
    ways = [
        name
        for name, value in (
            ("markdown", message.markdown),
            ("html", message.html),
            ("blocks", message.blocks),
        )
        if value is not None
    ]
    if len(ways) != 1:
        raise RichMessageError(
            "InputRichMessage: задайте ровно один способ — markdown, html "
            f"или blocks (задано: {', '.join(ways) or 'ничего'})."
        )
    if message.markdown is not None:
        _require_text(message.markdown, "InputRichMessage.markdown")
    elif message.html is not None:
        _require_text(message.html, "InputRichMessage.html")
    else:
        _validate_blocks(message.blocks, depth=0)


def _validate_blocks(
    blocks: Any, *, depth: int, where: str = "InputRichMessage.blocks"
) -> None:
    if isinstance(blocks, (str, bytes)) or not isinstance(blocks, Sequence):
        raise RichMessageError(f"{where}: ожидается список блоков.")
    if not blocks:
        raise RichMessageError(f"{where}: список блоков пуст.")
    for block in blocks:
        _validate_block(block, depth=depth)


def _validate_block(block: Any, *, depth: int) -> None:
    if not isinstance(block, BLOCK_TYPES):
        raise RichMessageError(
            f"Неизвестный блок Rich Message: {type(block).__name__}."
        )
    if isinstance(block, InputRichBlockSectionHeading):
        _require_rich_text(block.text, "Heading.text")
        _require_size(block.size, "Heading.size")
    elif isinstance(block, InputRichBlockParagraph):
        _require_rich_text(block.text, "Paragraph.text")
    elif isinstance(block, InputRichBlockList):
        if isinstance(block.items, (str, bytes)) or not isinstance(
            block.items, Sequence
        ):
            raise RichMessageError(
                "List.items: ожидается список InputRichBlockListItem."
            )
        if not block.items:
            raise RichMessageError("List.items: список пуст.")
        for item in block.items:
            _validate_list_item(item, depth=depth)
    elif isinstance(block, InputRichBlockDivider):
        return
    elif isinstance(block, InputRichBlockDetails):
        _require_rich_text(block.summary, "Details.summary")
        if block.is_open is not None and not isinstance(block.is_open, bool):
            raise RichMessageError("Details.is_open: ожидается true/false.")
        if depth + 1 > MAX_DETAILS_DEPTH:
            raise RichMessageError(
                "Слишком глубокая вложенность раскрываемых блоков "
                f"(максимум {MAX_DETAILS_DEPTH})."
            )
        _validate_blocks(block.blocks, depth=depth + 1)
    elif isinstance(block, InputRichBlockTable):
        _validate_table(block)
    elif isinstance(block, InputRichBlockBlockQuotation):
        _validate_blockquote(block, depth=depth)
    elif isinstance(block, InputRichBlockPhoto):
        _validate_photo(block)
    elif isinstance(block, InputRichBlockSlideshow):
        _validate_slideshow(block)


def _require_optional_rich_text(value: Any, what: str) -> None:
    """Необязательное `RichText`: `None` допустим, иначе — обычная проверка."""
    if value is not None:
        _require_rich_text(value, what)


def _require_optional_bool(value: Any, what: str) -> None:
    """Необязательный bool: `None` допустим; 0/1 и строки — нет."""
    if value is not None and not isinstance(value, bool):
        raise RichMessageError(f"{what}: ожидается true/false.")


def _require_optional_span(value: Any, what: str) -> None:
    """`colspan`/`rowspan`: необязательное целое ≥ 1 (bool не принимается)."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise RichMessageError(f"{what}: ожидается целое число ≥ 1.")
    if value < 1:
        raise RichMessageError(f"{what}: должно быть ≥ 1 (получено {value}).")


def _require_choice(value: Any, allowed: Sequence[str], what: str) -> None:
    """Значение обязательного enum-поля: только из списка допустимых."""
    if not isinstance(value, str) or value not in allowed:
        raise RichMessageError(
            f"{what}: ожидается одно из {', '.join(allowed)} " f"(получено {value!r})."
        )


def _validate_table(block: InputRichBlockTable) -> None:
    """Таблица: непустые строки из непустых ячеек с обязательными align/valign."""
    rows = block.cells
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise RichMessageError("Table.cells: ожидается список строк таблицы.")
    if not rows:
        raise RichMessageError("Table.cells: таблица без строк.")
    for row_index, row in enumerate(rows):
        where = f"Table.cells[{row_index}]"
        if isinstance(row, (str, bytes)) or not isinstance(row, Sequence):
            raise RichMessageError(f"{where}: ожидается список ячеек.")
        if not row:
            raise RichMessageError(f"{where}: строка таблицы пуста.")
        for cell_index, cell in enumerate(row):
            _validate_table_cell(cell, f"{where}[{cell_index}]")
    for name in ("is_bordered", "is_striped", "is_compact"):
        _require_optional_bool(getattr(block, name), f"Table.{name}")
    _require_optional_rich_text(block.caption, "Table.caption")


def _validate_table_cell(cell: Any, where: str) -> None:
    """Ячейка таблицы: обязательные enum-выравнивания и необязательные поля."""
    if not isinstance(cell, InputRichBlockTableCell):
        raise RichMessageError(
            f"{where}: ожидается InputRichBlockTableCell, "
            f"получено {type(cell).__name__}."
        )
    _require_choice(cell.align, TABLE_ALIGNMENTS, f"{where}.align")
    _require_choice(cell.valign, TABLE_VALIGNMENTS, f"{where}.valign")
    _require_optional_rich_text(cell.text, f"{where}.text")
    _require_optional_bool(cell.is_header, f"{where}.is_header")
    _require_optional_span(cell.colspan, f"{where}.colspan")
    _require_optional_span(cell.rowspan, f"{where}.rowspan")


def _validate_blockquote(block: InputRichBlockBlockQuotation, *, depth: int) -> None:
    """Цитата: обязательное непустое содержимое и необязательный автор."""
    _validate_blocks(block.blocks, depth=depth, where="BlockQuotation.blocks")
    _require_optional_rich_text(block.credit, "BlockQuotation.credit")


def _validate_photo(block: InputRichBlockPhoto) -> None:
    """
    Фото-блок: ровно `InputMediaPhoto` и непустой источник медиа.

    Строка-`file_id`, `InputFile` и байты допустимы; пустая строка и
    произвольный объект — нет (иначе ошибку вернёт сервер после загрузки).
    """
    photo = block.photo
    if not isinstance(photo, InputMediaPhoto):
        raise RichMessageError(
            "Photo.photo: ожидается telegram.InputMediaPhoto, "
            f"получено {type(photo).__name__}."
        )
    media = photo.media
    if isinstance(media, str):
        if not media.strip():
            raise RichMessageError("Photo.photo.media: пустой file_id.")
    elif not isinstance(media, (InputFile, bytes)):
        raise RichMessageError(
            "Photo.photo.media: ожидается file_id, InputFile или байты, "
            f"получено {type(media).__name__}."
        )
    if block.caption is not None:
        _validate_caption(block.caption, "Photo.caption")


def _validate_caption(caption: Any, where: str) -> None:
    """`RichBlockCaption`: обязательный текст, необязательный автор."""
    if not isinstance(caption, RichBlockCaption):
        raise RichMessageError(
            f"{where}: ожидается RichBlockCaption, получено {type(caption).__name__}."
        )
    _require_rich_text(caption.text, f"{where}.text")
    _require_optional_rich_text(caption.credit, f"{where}.credit")


def _validate_slideshow(block: InputRichBlockSlideshow) -> None:
    """Слайд-шоу: непустой список ТОЛЬКО фото-блоков и необязательная подпись."""
    blocks = block.blocks
    if isinstance(blocks, (str, bytes)) or not isinstance(blocks, Sequence):
        raise RichMessageError("Slideshow.blocks: ожидается список фото-блоков.")
    if not blocks:
        raise RichMessageError("Slideshow.blocks: слайд-шоу без кадров.")
    for index, child in enumerate(blocks):
        if not isinstance(child, InputRichBlockPhoto):
            raise RichMessageError(
                "Slideshow.blocks: допустимы только InputRichBlockPhoto, "
                f"получено {type(child).__name__} (позиция {index})."
            )
        _validate_photo(child)
    if block.caption is not None:
        _validate_caption(block.caption, "Slideshow.caption")


def _validate_list_item(item: Any, *, depth: int) -> None:
    """Элемент списка: обязательный непустой список валидных блоков."""
    if not isinstance(item, InputRichBlockListItem):
        raise RichMessageError(
            "List.items: ожидается InputRichBlockListItem, "
            f"получено {type(item).__name__}."
        )
    _validate_blocks(item.blocks, depth=depth, where="ListItem.blocks")


def _require_size(value: Any, what: str) -> int:
    """`size` заголовка: целое 1–6 (1 — крупнейший), bool не принимается."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise RichMessageError(f"{what}: ожидается целое число 1–6.")
    if not 1 <= value <= 6:
        raise RichMessageError(f"{what}: вне диапазона 1–6 (получено {value}).")
    return value


# —— отправка ————————————————————————————————————————————————————————


def _raise_api_error(response: Mapping[str, Any]) -> NoReturn:
    """
    Превращает `ok=false` из ответа Bot API в ошибку PTB.

    Штатный `Bot.do_api_request` делает это сам (разбор HTTP-кода), но
    адаптер обязан сохранить контракт и для «сырого» ответа: тесты и
    альтернативные клиенты возвращают JSON. Токен сюда не попадает и в
    текст ошибки не добавляется.
    """
    code = response.get("error_code")
    description = str(
        response.get("description") or "Telegram отклонил sendRichMessage"
    )
    parameters = response.get("parameters")
    if isinstance(parameters, Mapping):
        retry_after = parameters.get("retry_after")
        if retry_after is not None:
            raise RetryAfter(int(retry_after))
    if code == 403:
        raise Forbidden(description)
    if code in (401, 404):
        raise InvalidToken(description)
    if code == 409:
        raise Conflict(description)
    if code == 429:
        raise RetryAfter(1)
    if code == 400:
        raise BadRequest(description)
    raise NetworkError(description)


def _message_from_result(bot: Any, result: Any) -> Message | int:
    """
    Приводит ответ Bot API к `Message` (или хотя бы к `message_id`).

    Реальный PTB с `return_type=Message` уже отдаёт `Message`; «сырой»
    JSON и целое число поддержаны для тестов и альтернативных клиентов.
    """
    if isinstance(result, Message):
        return result
    if isinstance(result, bool):
        raise TelegramError("Bot API вернул неожиданный ответ на sendRichMessage.")
    if isinstance(result, int):
        return result
    if isinstance(result, Mapping):
        if result.get("ok") is False:
            _raise_api_error(result)
        payload = result.get("result", result)
        if isinstance(payload, Mapping) and "message_id" in payload:
            try:
                return Message.de_json(dict(payload), bot)
            except Exception as e:  # noqa: BLE001 — ответ сервера, не наш контракт
                raise TelegramError(
                    f"Не удалось разобрать ответ sendRichMessage: {e}"
                ) from e
        if isinstance(payload, int) and not isinstance(payload, bool):
            return payload
    raise TelegramError("Bot API вернул неожиданный ответ на sendRichMessage.")


# Префикс имени файла на верхнем уровне `api_kwargs`. PTB ждёт InputFile
# именно там, а `attach://`-ссылка остаётся внутри JSON `rich_message`.
RICH_MEDIA_PREFIX = "rich_media_"


def _hoist_input_files(value: Any, files: list[tuple[str, InputFile]]) -> Any:
    """
    Поднимает `InputFile` из вложенного payload на верхний уровень запроса.

    PTB `RequestParameter.from_input` разбирает только ключи верхнего уровня
    `api_kwargs`: `InputFile`, лежащий внутри обычного словаря, он отдаёт
    `json.dumps`, и сериализация падает с `TypeError`. Поэтому каждый файл
    получает своё имя (`rich_media_0`, …), ссылка `attach://<имя>` встаёт на
    место медиа, а сам файл уходит отдельным параметром.

    Возвращает новую структуру: исходный payload и `InputMediaPhoto` не
    меняются. `media` внутри `photo` — это `InputFile` (или строка/байты),
    `thumbnail` в фото-блоках не используется, но обрабатывается на случай
    будущих блоков с ним.
    """
    if isinstance(value, Mapping):
        hoisted: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, InputFile):
                name = f"{RICH_MEDIA_PREFIX}{len(files)}"
                files.append((name, item))
                hoisted[key] = item.attach_uri or f"attach://{name}"
            else:
                hoisted[key] = _hoist_input_files(item, files)
        return hoisted
    if isinstance(value, (str, bytes, bytearray)):
        return value
    if isinstance(value, Sequence):
        return [_hoist_input_files(item, files) for item in value]
    return value


async def send_rich_message(
    bot: Any,
    chat_id: int | str,
    rich_message: InputRichMessage | Mapping[str, Any],
    reply_markup: Any = None,
) -> Message | int:
    """
    Отправляет Rich Message официальным методом `sendRichMessage`.

    Это единственное место, где приложение вызывает Bot API напрямую.
    Используется публичный `Bot.do_api_request` — штатный PTB-механизм для
    методов, которых библиотека ещё не знает: токен, HTTP-клиент, таймауты
    и разбор ошибок остаются общими с остальным ботом (`Application`), а
    дублирования токена и второго сетевого клиента нет (§50).

    Возвращает `telegram.Message`, а если ответ неполный — `message_id`.
    """
    payload = (
        rich_message.to_dict()
        if isinstance(rich_message, InputRichMessage)
        else dict(rich_message)
    )
    # Проверка ДО сети: невалидная структура — ошибка вызывающего кода,
    # а не отказ Telegram.
    validate_rich_message(payload)

    # Файлы слайд-шоу: PTB достаёт InputFile только с верхнего уровня
    # api_kwargs, поэтому вложенное медиа поднимаем, а в JSON оставляем
    # attach://-ссылку. Payload без файлов проходит путь байт-в-байт прежним.
    media_files: list[tuple[str, InputFile]] = []
    payload = _hoist_input_files(payload, media_files)

    api_kwargs: dict[str, Any] = {"chat_id": chat_id, "rich_message": payload}
    for name, input_file in media_files:
        api_kwargs[name] = input_file
    if reply_markup is not None:
        api_kwargs["reply_markup"] = reply_markup

    do_api_request = getattr(bot, "do_api_request", None)
    if not callable(do_api_request):
        # PTB старше 20.8 (или тестовый двойник без произвольных эндпоинтов)
        # не умеет этот путь. Молча подменять отправку обычным sendMessage
        # нельзя: это была бы подделка Rich Message (§54).
        raise TelegramError(
            "Текущий клиент бота не поддерживает произвольные эндпоинты Bot API "
            "(нужен python-telegram-bot >= 20.8)."
        )

    result = await do_api_request(
        SEND_RICH_MESSAGE_ENDPOINT,
        api_kwargs=api_kwargs,
        return_type=Message,
    )
    message = _message_from_result(bot, result)
    logger.info("Rich Message отправлено (chat_id=%s).", chat_id)
    return message
