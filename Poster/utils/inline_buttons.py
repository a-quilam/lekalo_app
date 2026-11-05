# utils/inline_buttons.py
"""
Общий helper inline-кнопок Poster: цветовые стили и НАСТОЯЩИЕ неактивные
кнопки (Bot API 10.3, тип `DisabledButton`).

Зачем модуль
============

Экраны собирали `InlineKeyboardButton(...)` каждый по-своему, а «неактивный»
индикатор пагинации (`2/4`) до сих пор остаётся обычной кликабельной кнопкой
с no-op callback'ом: библиотека `python-telegram-bot` 22.8 знает параметр
`style`, но не знает поля `disabled`. Здесь три вещи в одном месте:

- `button(text, callback_data, *, style=...)` — обычная кнопка, стиль
  необязателен и по умолчанию не ставится (нейтральный вид клиента);
- `danger_button(text, callback_data)` — короткая запись для
  разрушительного/отменяющего действия (красный стиль);
- `disabled_button(text)` — информационная подсказка, которую нельзя нажать.

Как сериализуется `disabled`
============================

Библиотеку не обновляем и не патчим: у живого объекта `TelegramObject` есть
штатный канал для полей, которых ещё нет в PTB, — `api_kwargs`:

- `TelegramObject.to_dict()` «распаковывает» `api_kwargs` в результат
  (`out.update(out.pop("api_kwargs", {}))`);
- `InlineKeyboardMarkup.to_dict()` рекурсивно вызывает `to_dict()` каждой
  кнопки;
- `Bot._do_post` строит тело запроса через
  `RequestParameter.from_input` → `RequestData` → `telegram_object.to_dict()`.

То есть `api_kwargs={"disabled": {}}` попадает в JSON-параметр `reply_markup`
реального запроса к Bot API без единой правки библиотеки. Это проверяет
`tests/test_inline_buttons.py` на настоящем `Bot` с подменённым HTTP-слоем.

Значение — пустой объект `{}`, как в схеме (`disabled` имеет тип
`DisabledButton`, «currently holds no information»), а не `true`:
официальный сервер Bot API проверяет ФАКТ наличия поля
(`Client::get_inline_keyboard_button_type`), но схема типа — объект.

Почему у неактивной кнопки НЕТ callback_data
============================================

Сервер Bot API определяет тип кнопки по ПЕРВОМУ найденному полю: `url`,
`callback_data`, `callback_game`, `pay`, `switch_inline_query*`, `copy_text`
и только потом `disabled`. Кнопка `disabled` с `callback_data` превращается
в обычную кликабельную callback-кнопку — Telegram просто проигнорирует
`disabled`. Поэтому `disabled_button()` не ставит `callback_data` вовсе:
нажатие физически не порождает callback, бот ничего не отвечает и не меняет,
а клиент, не знающий `disabled`, показывает кнопку, которая ничего не делает.

Отдельный «молчащий» префикс не нужен: нажатия просто не приходят.
Существующий `handlers/noop.py` (индикатор пагинации) при этом остаётся
прежним — он отвечает коротким `answer()` без текста, то есть без алерта
(«молча»), и состояние не меняет.

Стили
=====

Обычные подсказки — без стиля. `danger` — только для разрушительных и
отменяющих действий. `primary`/`success` используются по мере применения в
релизе через `button(..., style=STYLE_PRIMARY)`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from telegram import InlineKeyboardButton

STYLE_PRIMARY: Final[str] = "primary"
"""Синий стиль — основное действие экрана (Bot API `style`)."""

STYLE_SUCCESS: Final[str] = "success"
"""Зелёный стиль — подтверждение/успешное завершение (Bot API `style`)."""

STYLE_DANGER: Final[str] = "danger"
"""Красный стиль — только разрушительное или отменяющее действие."""

BUTTON_STYLES: Final[frozenset[str]] = frozenset(
    {STYLE_PRIMARY, STYLE_SUCCESS, STYLE_DANGER}
)
"""Допустимые значения `style`: остальные Telegram отклонит."""

DISABLED_FIELD: Final[str] = "disabled"
"""Поле Bot API 10.3 «кнопка не нажимается» (тип `DisabledButton`)."""


def _checked_style(style: str | None) -> str | None:
    """Проверяет стиль заранее: опечатка не должна дойти до запроса Telegram."""
    if style is None:
        return None
    value = str(style).strip().lower()
    if value not in BUTTON_STYLES:
        raise ValueError(
            f"Неизвестный стиль кнопки: {style!r}; "
            f"допустимо: {', '.join(sorted(BUTTON_STYLES))}"
        )
    return value


def button(
    text: str,
    callback_data: str,
    *,
    style: str | None = None,
) -> InlineKeyboardButton:
    """
    Обычная (нажимаемая) кнопка с подписью, callback и необязательным стилем.

    Стиль по умолчанию не ставится: клиент рисует кнопку своим нейтральным
    оформлением, а JSON запроса не содержит лишнего поля `style`.
    """
    return InlineKeyboardButton(
        str(text),
        callback_data=str(callback_data),
        style=_checked_style(style),
    )


def danger_button(text: str, callback_data: str) -> InlineKeyboardButton:
    """Кнопка разрушительного/отменяющего действия (красный стиль)."""
    return button(text, callback_data, style=STYLE_DANGER)


def disabled_button(text: str) -> InlineKeyboardButton:
    """
    Неактивная информационная подсказка — «нажать нельзя, действия нет».

    `callback_data` намеренно отсутствует: иначе сервер Bot API (он выбирает
    тип кнопки по первому найденному полю) сделал бы кнопку обычной
    кликабельной и проигнорировал `disabled`. Поэтому нажатие не порождает
    callback и бот гарантированно молчит — ни алерта, ни изменения состояния.
    """
    return InlineKeyboardButton(
        str(text),
        api_kwargs={DISABLED_FIELD: {}},
    )


def is_disabled(item: Any) -> bool:
    """
    Неактивная ли кнопка (для защитных проверок в обработчиках).

    Признак — поле `disabled` в `api_kwargs`: так кнопка выглядит и сразу
    после сборки, и после десериализации ответа Telegram.
    """
    kwargs = getattr(item, "api_kwargs", None)
    if not isinstance(kwargs, Mapping):
        return False
    return DISABLED_FIELD in kwargs


__all__ = [
    "BUTTON_STYLES",
    "DISABLED_FIELD",
    "STYLE_DANGER",
    "STYLE_PRIMARY",
    "STYLE_SUCCESS",
    "button",
    "danger_button",
    "disabled_button",
    "is_disabled",
]
