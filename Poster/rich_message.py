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

Rich Message используется ИСКЛЮЧИТЕЛЬНО для справки (§46): анонсы
отправляются обычными сообщениями/альбомами.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NoReturn

from telegram import Message
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
class InputRichBlockSectionHeading:
    """Заголовок раздела (`InputRichBlockSectionHeading`, `type: "heading"`)."""

    text: str
    # 1 — самый крупный заголовок, 6 — самый мелкий (официальная схема).
    size: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"type": "heading", "text": self.text, "size": self.size}


@dataclass(frozen=True)
class InputRichBlockParagraph:
    """Абзац текста (`InputRichBlockParagraph`, §49)."""

    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"type": "paragraph", "text": self.text}


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

    summary: str
    blocks: Sequence[RichBlock] = field(default_factory=tuple)
    is_open: bool | None = None

    def to_dict(self, *, depth: int = 0) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "details",
            "summary": self.summary,
            "blocks": [block_to_dict(block, depth=depth + 1) for block in self.blocks],
        }
        if self.is_open:
            payload["is_open"] = True
        return payload


RichBlock = (
    InputRichBlockSectionHeading
    | InputRichBlockParagraph
    | InputRichBlockList
    | InputRichBlockDivider
    | InputRichBlockDetails
)

# Все классы блоков — для проверки «это вообще известный блок».
BLOCK_TYPES: tuple[type, ...] = (
    InputRichBlockSectionHeading,
    InputRichBlockParagraph,
    InputRichBlockList,
    InputRichBlockDivider,
    InputRichBlockDetails,
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
    return block.to_dict()


# —— проверка структуры (до сети) ————————————————————————————————————


def _require_text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RichMessageError(f"{what}: ожидается непустая строка.")
    return value


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
        _require_text(block.text, "Heading.text")
        _require_size(block.size, "Heading.size")
    elif isinstance(block, InputRichBlockParagraph):
        _require_text(block.text, "Paragraph.text")
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
        _require_text(block.summary, "Details.summary")
        if block.is_open is not None and not isinstance(block.is_open, bool):
            raise RichMessageError("Details.is_open: ожидается true/false.")
        if depth + 1 > MAX_DETAILS_DEPTH:
            raise RichMessageError(
                "Слишком глубокая вложенность раскрываемых блоков "
                f"(максимум {MAX_DETAILS_DEPTH})."
            )
        _validate_blocks(block.blocks, depth=depth + 1)


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

    api_kwargs: dict[str, Any] = {"chat_id": chat_id, "rich_message": payload}
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
