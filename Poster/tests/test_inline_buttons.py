# tests/test_inline_buttons.py
"""
Тесты общего helper'а inline-кнопок Poster (`utils/inline_buttons.py`).

Проверяется то, что действительно уходит в Telegram, а не только то, что
«объект создался»:

- обычная кнопка: `callback_data` и `text` без лишних полей, стиль
  появляется в JSON только когда он задан, неизвестный стиль отклоняется
  заранее;
- неактивная кнопка: в `to_dict()`/`to_json()` есть поле `disabled` и НЕТ
  `callback_data` (иначе сервер Bot API сделал бы её кликабельной);
- РЕАЛЬНЫЙ путь запроса: `Bot.send_message` с настоящим `Bot` и подменённым
  HTTP-слоем — параметр `reply_markup` в теле запроса содержит
  `"disabled": {}`;
- no-op обработчик пагинации отвечает молча (без текста алерта) и ничего
  не редактирует;
- существующие раскладки (`keyboard_layouts`, `pagination`) не получили
  регрессий: их кнопки остаются обычными нажимаемыми.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_inline_buttons -v
"""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.request import BaseRequest

from handlers import noop as noop_module
from utils import inline_buttons, keyboard_layouts, pagination


class _CapturingRequest(BaseRequest):
    """HTTP-слой Bot, который запоминает тело запроса и не ходит в сеть."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, Any]] = []

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    @property
    def read_timeout(self) -> float:
        return 5.0

    @property
    def write_timeout(self) -> float:
        return 5.0

    @property
    def connect_timeout(self) -> float:
        return 5.0

    @property
    def pool_timeout(self) -> float:
        return 1.0

    async def do_request(
        self,
        url: str,
        method: str,
        request_data: Any = None,
        read_timeout: Any = None,
        write_timeout: Any = None,
        connect_timeout: Any = None,
        pool_timeout: Any = None,
    ) -> tuple[int, bytes]:
        self.requests.append((url, request_data))
        body = json.dumps(
            {
                "ok": True,
                "result": {
                    "message_id": 1,
                    "date": 0,
                    "chat": {"id": 5, "type": "private"},
                    "text": "экран",
                },
            }
        ).encode()
        return 200, body


def _reply_markup_json(request: Any) -> Any:
    """`reply_markup` из тела запроса, как его увидит Bot API (строка JSON)."""
    return json.loads(request.json_parameters["reply_markup"])


class ButtonTests(unittest.TestCase):
    """Обычные кнопки: стиль только по запросу, поведение прежнее."""

    def test_plain_button_has_no_style_and_keeps_callback(self) -> None:
        item = inline_buttons.button("◀️ Назад", "pl_page:0")
        payload = item.to_dict()
        self.assertEqual(payload["text"], "◀️ Назад")
        self.assertEqual(payload["callback_data"], "pl_page:0")
        self.assertNotIn("style", payload)
        self.assertNotIn("disabled", payload)
        self.assertFalse(inline_buttons.is_disabled(item))

    def test_style_is_serialized(self) -> None:
        for style in (
            inline_buttons.STYLE_PRIMARY,
            inline_buttons.STYLE_SUCCESS,
            inline_buttons.STYLE_DANGER,
        ):
            with self.subTest(style=style):
                item = inline_buttons.button("Да", "yes", style=style)
                self.assertEqual(item.to_dict()["style"], style)
                markup = InlineKeyboardMarkup([[item]])
                self.assertIn(f'"style": "{style}"', markup.to_json())

    def test_danger_button_uses_danger_style(self) -> None:
        item = inline_buttons.danger_button("🗑 Удалить", "pl_del:3")
        self.assertEqual(item.to_dict()["style"], "danger")
        self.assertEqual(item.callback_data, "pl_del:3")
        self.assertFalse(inline_buttons.is_disabled(item))

    def test_unknown_style_rejected_before_request(self) -> None:
        with self.assertRaises(ValueError):
            inline_buttons.button("Да", "yes", style="pink")

    def test_style_is_case_and_space_insensitive(self) -> None:
        item = inline_buttons.button("Да", "yes", style=" DANGER ")
        self.assertEqual(item.to_dict()["style"], "danger")

    def test_button_stays_a_plain_ptb_button(self) -> None:
        # Совместимость: существующий код ждёт именно InlineKeyboardButton
        # (раскладки, проверки типов InlineKeyboardMarkup).
        item = inline_buttons.button("Да", "yes")
        self.assertIs(type(item), InlineKeyboardButton)
        InlineKeyboardMarkup([[item]])  # не бросает: тип клавиатуры верный


class DisabledButtonTests(unittest.TestCase):
    """Неактивная кнопка: `disabled` в сериализации, никакого callback."""

    def test_disabled_field_serialized(self) -> None:
        item = inline_buttons.disabled_button("2/4")
        payload = item.to_dict()
        self.assertEqual(payload["text"], "2/4")
        self.assertIn("disabled", payload)
        self.assertTrue(inline_buttons.is_disabled(item))

    def test_disabled_button_has_no_callback_data(self) -> None:
        # Сервер Bot API выбирает тип кнопки по первому найденному полю:
        # `callback_data` раньше `disabled` сделал бы кнопку кликабельной.
        payload = inline_buttons.disabled_button("2/4").to_dict()
        for field in (
            "callback_data",
            "url",
            "web_app",
            "pay",
            "switch_inline_query",
            "copy_text",
        ):
            self.assertNotIn(field, payload)

    def test_disabled_in_markup_json(self) -> None:
        markup = InlineKeyboardMarkup(
            [
                [
                    inline_buttons.button("◀️", "pl_page:0"),
                    inline_buttons.disabled_button("2/4"),
                ]
            ]
        )
        decoded = json.loads(markup.to_json())
        row = decoded["inline_keyboard"][0]
        self.assertEqual(row[0]["callback_data"], "pl_page:0")
        self.assertNotIn("disabled", row[0])
        self.assertIn("disabled", row[1])
        self.assertNotIn("callback_data", row[1])

    def test_real_request_body_contains_disabled(self) -> None:
        """Настоящий Bot.send_message → тело запроса к Bot API."""
        request = _CapturingRequest()
        bot = Bot(token="123456:TEST-token", request=request)

        async def run() -> None:
            await bot.send_message(
                chat_id=5,
                text="экран",
                reply_markup=InlineKeyboardMarkup(
                    [[inline_buttons.disabled_button("2/4")]]
                ),
            )

        asyncio.run(run())
        self.assertEqual(len(request.requests), 1)
        url, request_data = request.requests[0]
        self.assertTrue(url.endswith("/sendMessage"), url)
        markup = _reply_markup_json(request_data)
        button = markup["inline_keyboard"][0][0]
        self.assertEqual(button["text"], "2/4")
        self.assertIn("disabled", button)
        self.assertNotIn("callback_data", button)

    def test_disabled_json_is_schema_object(self) -> None:
        # В схеме тип `disabled` — DisabledButton (объект), а не Boolean.
        payload = json.loads(inline_buttons.disabled_button("2/4").to_json())
        self.assertIsInstance(payload["disabled"], dict)

    def test_plain_button_not_considered_disabled(self) -> None:
        self.assertFalse(inline_buttons.is_disabled(inline_buttons.button("a", "b")))

    def test_disabled_survives_deserialization(self) -> None:
        # Кнопка, вернувшаяся из ответа Telegram, остаётся опознаваемой:
        # защитные проверки в обработчиках не зависят от способа получения.
        item = InlineKeyboardButton.de_json({"text": "2/4", "disabled": {}}, None)
        assert item is not None
        self.assertTrue(inline_buttons.is_disabled(item))


class NoopHandlerTests(unittest.TestCase):
    """Индикатор пагинации: молчание, без алерта и без правок сообщения."""

    def test_noop_answers_without_text(self) -> None:
        answered: list[dict[str, Any]] = []

        async def answer(*args: Any, **kwargs: Any) -> None:
            answered.append(kwargs)

        update: Any = SimpleNamespace(callback_query=SimpleNamespace(answer=answer))
        context: Any = SimpleNamespace(bot=None)
        asyncio.run(noop_module.handle_noop(update, context))

        self.assertEqual(len(answered), 1)
        # Ни текста, ни показа алерта: нажатие индикатора остаётся молчаливым.
        self.assertFalse(answered[0].get("text"))
        self.assertFalse(answered[0].get("show_alert"))

    def test_noop_pattern_does_not_match_disabled_lookalike(self) -> None:
        # Неактивная кнопка не порождает callback вовсе; служебный префикс
        # noop остаётся только у индикатора с числовым значением.
        import re

        self.assertIsNone(re.match(noop_module.NOOP_PATTERN, "disabled"))
        self.assertIsNone(re.match(noop_module.NOOP_PATTERN, "noop:"))
        self.assertIsNotNone(re.match(noop_module.NOOP_PATTERN, "noop:2:4"))


class ExistingLayoutsTests(unittest.TestCase):
    """Массовой миграции нет: прежние раскладки ведут себя как раньше."""

    def test_keyboard_layouts_button_still_plain(self) -> None:
        item = keyboard_layouts.button("📨", "bp_start:7")
        payload = item.to_dict()
        self.assertEqual(payload["callback_data"], "bp_start:7")
        self.assertNotIn("style", payload)
        self.assertNotIn("disabled", payload)

    def test_pagination_indicator_still_ordinary_button(self) -> None:
        row = pagination.nav_buttons(1, 4, "pl_page")
        assert row is not None
        calls = [data for _label, data in row]
        # Протокол «смещение» прежний: 2-я страница 4-страничного списка.
        self.assertEqual(calls, ["pl_page:0", "noop:2:4", "pl_page:10"])

    def test_pagination_keyboard_buttons_are_clickable(self) -> None:
        row = pagination.nav_keyboard(1, 4, "pl_page")
        assert row is not None
        self.assertTrue(all(not inline_buttons.is_disabled(item) for item in row))


if __name__ == "__main__":
    unittest.main()
