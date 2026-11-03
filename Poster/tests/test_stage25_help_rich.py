# tests/test_stage25_help_rich.py
"""
Poster 2.5, этап E (§46–54, §60–61): справка как настоящий Rich Message.

Что закрепляется:

(а) структура справки `build_help_rich_message`: способ `blocks`, заголовок,
    введение, ровно десять СВЁРНУТЫХ `InputRichBlockDetails`, непустые
    названия разделов, узнаваемые ключевые слова §51;
(б) адаптер `rich_message.send_rich_message`: успешный ответ даёт
    `Message`/`message_id`, `ok=false` превращается в ошибку Telegram,
    невалидная структура отвергается ДО сети, токен не попадает в логи и
    текст ошибки;
(в) `/help` через настоящее `Application.process_update`: ровно ОДИН
    `sendRichMessage` (не `sendMessage`), корректный `rich_message`,
    ровно одна кнопка возврата «◀️ Назад» (`help_back`) в `reply_markup`;
(г) возврат «◀️ Назад» удаляет временное сообщение справки (delete_message)
    и перерисовывает экран-источник (Poster 2.5.1), анонсы/прочие данные
    не затрагиваются;
(д) другие разделы Rich Message не используют (§54).

Реальные Telegram, `.env` и `post_bot.db` не используются: FakeBot,
временная SQLite (tests.integration_harness) и подменённый сетевой вызов.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock

from telegram import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

try:
    from help_rich import build_help_rich_message
    from rich_message import (
        MAX_DETAILS_DEPTH,
        InputRichBlockDetails,
        InputRichBlockDivider,
        InputRichBlockList,
        InputRichBlockListItem,
        InputRichBlockParagraph,
        InputRichBlockSectionHeading,
        InputRichMessage,
        RichMessageError,
        send_rich_message,
        validate_rich_message,
    )
    from tests.integration_harness import (
        FIXTURE_DATE,
        HARNESS_IMPORT_ERROR,
        Harness,
        inline_buttons,
        scenario_test,
    )
    from utils import ui_texts

    IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    IMPORT_ERROR = str(exc)


# §53 + Poster 2.5.1: единственная кнопка возврата из справки — «◀️ Назад»
# (`help_back`), старая «🏠 В меню» (`main_menu`) с Rich Message убрана.
BACK_LABEL = "◀️ Назад"
BACK_DATA = "help_back"


class StubBot:
    """
    Минимальный бот для проверки адаптера: запоминает вызов эндпоинта.

    `answer` — то, что вернёт `do_api_request` (реальный PTB возвращает уже
    разобранный результат), `error` — исключение вместо ответа.
    """

    def __init__(self, answer: Any = None, error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.endpoint: str | None = None
        self.api_kwargs: dict[str, Any] | None = None

    @property
    def sent(self) -> dict[str, Any]:
        """Параметры последнего вызова (проверено, что вызов был)."""
        assert self.api_kwargs is not None
        return self.api_kwargs

    async def do_api_request(
        self,
        endpoint: str,
        api_kwargs: dict[str, Any] | None = None,
        return_type: Any = None,
        **kwargs: Any,
    ) -> Any:
        self.endpoint = endpoint
        self.api_kwargs = api_kwargs
        if self.error is not None:
            raise self.error
        return self.answer


def _message(message_id: int = 500) -> Message:
    return Message(
        message_id=message_id,
        date=FIXTURE_DATE,
        chat=Chat(id=777, type="private"),
        text=None,
    )


def _blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Блоки готового словаря Bot API (проверка структуры для mypy)."""
    blocks = payload["blocks"]
    assert isinstance(blocks, list)
    return blocks


def _run(coro):
    return asyncio.run(coro)


def _list_item_texts(block: InputRichBlockList) -> list[str]:
    """Тексты элементов списка: `items[].blocks[]` → абзацы."""
    texts: list[str] = []
    for item in block.items:
        for child in item.blocks:
            if isinstance(child, InputRichBlockParagraph):
                texts.append(child.text)
    return texts


# —— 1. Структура справки ————————————————————————————————————————————


class HelpStructureTests(unittest.TestCase):
    """§48–49, §51–52: справка — blocks с десятью свёрнутыми разделами."""

    def setUp(self) -> None:
        self.rich = build_help_rich_message()
        blocks = self.rich.blocks
        assert blocks is not None
        self.blocks = list(blocks)

    def _details(self) -> list[InputRichBlockDetails]:
        return [b for b in self.blocks if isinstance(b, InputRichBlockDetails)]

    def test_structure_is_valid(self) -> None:
        validate_rich_message(self.rich)
        payload = self.rich.to_dict()
        self.assertEqual(list(payload), ["blocks"])
        self.assertTrue(_blocks(payload))

    def test_single_way_is_blocks(self) -> None:
        self.assertIsNone(self.rich.markdown)
        self.assertIsNone(self.rich.html)
        self.assertIsNotNone(self.rich.blocks)

    def test_heading_and_intro_are_present(self) -> None:
        payload = self.rich.to_dict()
        first = _blocks(payload)[0]
        # Официальная схема: type "heading", текст в поле text, size обязателен.
        self.assertEqual(first["type"], "heading")
        self.assertEqual(first["text"], "Poster — помощь")
        self.assertEqual(first["size"], 1)
        self.assertNotIn("heading", first)
        texts = [
            block.get("text", "")
            for block in _blocks(payload)
            if block["type"] == "paragraph"
        ]
        self.assertTrue(any("Poster помогает создавать анонсы" in t for t in texts))

    def test_exactly_ten_collapsed_details(self) -> None:
        details = self._details()
        self.assertEqual(len(details), 10)
        for block in details:
            self.assertIsNone(block.is_open, "раздел должен быть свёрнут (§52)")
            self.assertTrue(block.summary.strip(), "пустое название раздела")

    def test_details_are_real_expandable_blocks(self) -> None:
        payload = self.rich.to_dict()
        details = [b for b in _blocks(payload) if b["type"] == "details"]
        self.assertEqual(len(details), 10)
        for block in details:
            self.assertNotIn(
                "is_open",
                block,
                "свёрнутый блок не должен передавать is_open (схема Bot API)",
            )
            self.assertTrue(block["blocks"], "раскрываемый блок без содержимого")
            self.assertIn("summary", block)

    def test_section_titles_cover_spec(self) -> None:
        titles = [block.summary for block in self._details()]
        for expected in (
            "Создание анонса",
            "Площадки",
            "Шаблоны",
            "Кирпичики",
            "Дополнительные поля",
            "Условные части",
            "Фотографии",
            "Редактирование анонса",
            "Управление площадкой",
            "Команды",
        ):
            self.assertIn(expected, titles)

    def test_section_texts_mention_expected_keywords(self) -> None:
        """§54: тексты разделов действительно рассказывают о своём предмете."""
        by_title = {block.summary: block for block in self._details()}

        def text_of(title: str) -> str:
            block = by_title[title]
            parts = []
            for child in block.blocks:
                if isinstance(child, InputRichBlockParagraph):
                    parts.append(child.text)
                elif isinstance(child, InputRichBlockList):
                    parts.extend(_list_item_texts(child))
            return "\n".join(parts)

        self.assertIn("Создать анонс", text_of("Создание анонса"))
        self.assertIn("тире после времени начала", text_of("Условные части"))
        self.assertIn("программные выражения", text_of("Условные части"))
        self.assertIn("расширенный", text_of("Дополнительные поля"))
        self.assertIn("[дата]", text_of("Кирпичики"))
        commands = text_of("Команды")
        self.assertIn("/start", commands)
        self.assertIn("/help", commands)

    def test_section_texts_use_catalog_button_labels(self) -> None:
        """
        MINOR-2 приёмки E: упоминания кнопок берутся из каталога.

        Проверка через подмену каталога: если в тексте раздела осталась
        жёсткая подпись, справка начнёт называть кнопку по-старому. Заодно
        закрепляется, что каждая подпись — существующий ключ каталога.
        """
        from help_rich import _LABELS

        for token, key in _LABELS.items():
            self.assertIn(key, ui_texts.DEFAULTS, f"{token}: нет ключа {key}")

        catalog = ui_texts.texts()
        overrides = {
            "buttons.finished.data": "📝 ДАННЫЕ-ПРОВЕРКА",
            "buttons.finished.photos": "🖼 СНИМКИ ({count})",
            "wizard.photos.title": "📷 ФОТО-ШАГ-ПРОВЕРКА",
        }
        patched = dict(catalog, **overrides)
        with mock.patch.object(ui_texts, "_catalog", patched):
            rebuilt = build_help_rich_message()
        by_title = {
            block.summary: block
            for block in rebuilt.blocks or ()
            if isinstance(block, InputRichBlockDetails)
        }

        def text_of(title: str) -> str:
            parts: list[str] = []
            for child in by_title[title].blocks:
                if isinstance(child, InputRichBlockParagraph):
                    parts.append(child.text)
                elif isinstance(child, InputRichBlockList):
                    parts.extend(_list_item_texts(child))
            return "\n".join(parts)

        edited = text_of("Редактирование анонса")
        self.assertIn("📝 ДАННЫЕ-ПРОВЕРКА", edited)
        self.assertIn("🖼 СНИМКИ (n)", edited)
        self.assertNotIn("«Поля»", edited)
        self.assertNotIn("«Фотографии»", edited)
        self.assertIn("📷 ФОТО-ШАГ-ПРОВЕРКА", text_of("Фотографии"))

    def test_section_texts_have_no_leftover_markers(self) -> None:
        """
        В готовой справке нет `{token}`-маркеров и лишних фигурных скобок.

        Опорный инвариант подстановки подписей: если в тексте раздела
        появится `{...}` без ключа в `_LABELS`, `_fill` упадёт `KeyError`, а
        незамеченный маркер показался бы пользователю как есть.
        """
        from help_rich import _LABELS, _SECTIONS

        for _title_key, paragraph, items in _SECTIONS:
            for text in (paragraph, *items):
                for marker in re.findall(r"\{([^}]*)\}", text):
                    self.assertIn(
                        marker, _LABELS, f"неизвестная подстановка {{{marker}}}"
                    )

        for block in self._details():
            for child in block.blocks:
                values: list[str] = []
                if isinstance(child, InputRichBlockParagraph):
                    values.append(child.text)
                elif isinstance(child, InputRichBlockList):
                    values.extend(_list_item_texts(child))
                for value in values:
                    self.assertNotIn("{", value, "в справке остался маркер")
                    self.assertNotIn("}", value, "в справке остался маркер")

    def test_commands_section_lists_only_real_commands(self) -> None:
        """§51 п.10: в справочнике команд нет придуманных команд."""
        real = {
            "/start",
            "/help",
            "/new_wizard",
            "/drafts",
            "/places",
            "/templates",
            "/bricks",
            "/admin",
            "/cancel",
        }
        details = [block for block in self._details() if block.summary == "Команды"]
        self.assertEqual(len(details), 1)
        items: list[str] = []
        for child in details[0].blocks:
            if isinstance(child, InputRichBlockList):
                items.extend(_list_item_texts(child))
        listed = {
            token.split()[0].rstrip(",")
            for item in items
            for token in item.split()
            if token.startswith("/")
        }
        self.assertTrue(listed <= real, f"придуманные команды: {listed - real}")
        self.assertIn("/start", listed)


# —— 2. Валидация и адаптер ——————————————————————————————————————————


class ValidationTests(unittest.TestCase):
    """Невалидная структура отвергается ДО сети, с понятным текстом."""

    def test_two_ways_at_once_is_rejected(self) -> None:
        with self.assertRaises(RichMessageError):
            validate_rich_message(
                InputRichMessage(markdown="# x", blocks=(InputRichBlockDivider(),))
            )

    def test_no_way_is_rejected(self) -> None:
        with self.assertRaises(RichMessageError):
            validate_rich_message(InputRichMessage())

    def test_empty_heading_is_rejected(self) -> None:
        with self.assertRaises(RichMessageError):
            validate_rich_message(
                InputRichMessage(blocks=(InputRichBlockSectionHeading(text="  "),))
            )

    def test_empty_list_is_rejected(self) -> None:
        with self.assertRaises(RichMessageError):
            validate_rich_message(
                InputRichMessage(blocks=(InputRichBlockList(items=()),))
            )

    def test_list_item_with_empty_blocks_is_rejected(self) -> None:
        with self.assertRaises(RichMessageError):
            validate_rich_message(
                InputRichMessage(
                    blocks=(
                        InputRichBlockList(items=(InputRichBlockListItem(blocks=()),)),
                    )
                )
            )

    def test_empty_paragraph_is_rejected(self) -> None:
        with self.assertRaises(RichMessageError):
            validate_rich_message(
                InputRichMessage(blocks=(InputRichBlockParagraph(text=""),))
            )

    def test_too_deep_details_is_rejected(self) -> None:
        nested: Any = InputRichBlockParagraph(text="дно")
        for level in range(MAX_DETAILS_DEPTH + 1):
            nested = InputRichBlockDetails(summary=f"ур.{level}", blocks=(nested,))
        with self.assertRaises(RichMessageError):
            validate_rich_message(InputRichMessage(blocks=(nested,)))

    def test_error_is_value_error(self) -> None:
        self.assertTrue(issubclass(RichMessageError, ValueError))


class AdapterTests(unittest.TestCase):
    """§50: один сетевой вызов официального метода, без утечки токена."""

    def test_success_returns_message(self) -> None:
        bot = StubBot(answer=_message(501))
        result = _run(
            send_rich_message(
                bot, 777, InputRichMessage(blocks=(InputRichBlockDivider(),))
            )
        )
        self.assertIsInstance(result, Message)
        self.assertEqual(result.message_id, 501)
        self.assertEqual(bot.endpoint, "sendRichMessage")
        self.assertEqual(bot.sent["chat_id"], 777)
        self.assertEqual(bot.sent["rich_message"], {"blocks": [{"type": "divider"}]})
        self.assertNotIn("reply_markup", bot.sent)

    def test_reply_markup_is_passed_through(self) -> None:
        bot = StubBot(answer=_message())
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(BACK_LABEL, callback_data=BACK_DATA)]]
        )
        _run(
            send_rich_message(
                bot,
                777,
                InputRichMessage(markdown="привет"),
                reply_markup=keyboard,
            )
        )
        self.assertIs(bot.sent["reply_markup"], keyboard)

    def test_integer_result_is_returned_as_message_id(self) -> None:
        bot = StubBot(answer=42)
        self.assertEqual(
            _run(send_rich_message(bot, 1, InputRichMessage(html="<b>x</b>"))), 42
        )

    def test_raw_json_ok_false_raises_bad_request(self) -> None:
        bot = StubBot(
            answer={"ok": False, "error_code": 400, "description": "Bad Request: nope"}
        )
        with self.assertRaises(BadRequest):
            _run(send_rich_message(bot, 1, InputRichMessage(html="x")))

    def test_raw_json_forbidden_and_retry_after(self) -> None:
        bot = StubBot(answer={"ok": False, "error_code": 403, "description": "blocked"})
        with self.assertRaises(Forbidden):
            _run(send_rich_message(bot, 1, InputRichMessage(html="x")))

        bot = StubBot(
            answer={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 7},
            }
        )
        with self.assertRaises(RetryAfter):
            _run(send_rich_message(bot, 1, InputRichMessage(html="x")))

    def test_network_error_propagates(self) -> None:
        bot = StubBot(error=NetworkError("timed out"))
        with self.assertRaises(NetworkError):
            _run(send_rich_message(bot, 1, InputRichMessage(html="x")))

    def test_invalid_structure_never_reaches_network(self) -> None:
        bot = StubBot(answer=_message())
        with self.assertRaises(ValueError):
            _run(send_rich_message(bot, 1, InputRichMessage()))
        self.assertIsNone(bot.endpoint, "невалидная структура ушла в сеть")

    def test_token_is_not_in_error_text_or_logs(self) -> None:
        """
        Токен адаптер не читает и не печатает: его держит клиент PTB.

        Проверяем оба канала утечки: текст исключения Telegram и записи
        журнала адаптера (успешная отправка логируется).
        """
        secret = "123456:SECRET-TOKEN"
        bot = StubBot(
            answer={"ok": False, "error_code": 400, "description": "Bad Request"}
        )
        with self.assertRaises(BadRequest) as caught:
            _run(send_rich_message(bot, 1, InputRichMessage(html="x")))
        self.assertNotIn(secret, str(caught.exception))

        rich_adapter = __import__("rich_message")
        with self.assertLogs(rich_adapter.__name__, level=logging.INFO) as logs:
            _run(
                send_rich_message(
                    StubBot(answer=_message()), 1, InputRichMessage(html="x")
                )
            )
        self.assertTrue(logs.output)
        self.assertNotIn(secret, "\n".join(logs.output))

    def test_bot_without_do_api_request_is_reported(self) -> None:
        """§54: подменять Rich Message обычным sendMessage нельзя."""
        bot = SimpleNamespace()
        with self.assertRaises(Exception) as caught:
            _run(send_rich_message(bot, 1, InputRichMessage(html="x")))
        self.assertNotIsInstance(caught.exception, RichMessageError)


# —— 3. Справка через настоящее приложение —————————————————————————


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class HelpFlowTests(unittest.TestCase):
    """/help: один sendRichMessage, кнопка возврата, очистка, другие разделы."""

    @scenario_test
    async def test_help_sends_exactly_one_rich_message(self, h: Harness) -> None:
        await h.text("/start")
        messages_before = [c for c in h.calls if c["type"] == "message"]
        await h.callback("help")

        self.assertEqual(len(h.bot.rich_records), 1)
        record = h.bot.rich_records[0]
        self.assertEqual(record["chat_id"], h.chat_id)

        payload = record["rich_message"]
        validate_rich_message(payload)
        self.assertEqual(list(payload), ["blocks"])
        details = [b for b in _blocks(payload) if b["type"] == "details"]
        self.assertEqual(len(details), 10)
        self.assertTrue(all("is_open" not in block for block in details))

        labels = [label for label, _data in inline_buttons(record["reply_markup"])]
        self.assertEqual(labels, [BACK_LABEL], "на справке должна быть одна кнопка")

        # обычный sendMessage под видом Rich Message не отправлялся
        rich_calls = [c for c in h.calls if c["type"] == "rich_message"]
        self.assertEqual(len(rich_calls), 1)
        messages_after = [
            c for c in h.calls if c["type"] == "message" and c not in messages_before
        ]
        self.assertEqual(messages_after, [], "справка ушла обычным sendMessage")

    @scenario_test
    async def test_help_is_recorded_in_operation_journal(self, h: Harness) -> None:
        """§61: вызов учтён в общем журнале, а не только в rich_records."""
        await h.text("/start")
        await h.callback("help")
        rich = [c for c in h.calls if c["type"] == "rich_message"]
        self.assertEqual(len(rich), 1)
        self.assertEqual(rich[0]["rich_message"]["blocks"][0]["type"], "heading")

    @scenario_test
    async def test_back_returns_to_menu_and_deletes_help(self, h: Harness) -> None:
        """Poster 2.5.1: «◀️ Назад» из справки, открытой в меню, возвращает меню."""
        await h.text("/start")
        await h.callback("help")
        rich_id = h.bot.rich_records[0]["message_id"]
        self.assertEqual(h.user_data()["help_rich_message_id"], rich_id)

        await h.callback(BACK_DATA)

        self.assertIn(rich_id, h.deleted_ids(), "временная справка не удалена")
        self.assertNotIn("help_rich_message_id", h.user_data())
        self.assertIn("Что хотите сделать?", h.screen_text())
        self.assertEqual(len(h.bot.rich_records), 1, "лишний Rich Message")

    @scenario_test
    async def test_repeated_help_does_not_accumulate_messages(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        first_id = h.bot.rich_records[0]["message_id"]
        await h.text("/help")

        self.assertEqual(len(h.bot.rich_records), 2)
        self.assertIn(first_id, h.deleted_ids())
        self.assertNotEqual(h.user_data()["help_rich_message_id"], first_id)

    @scenario_test
    async def test_help_does_not_touch_announcements(self, h: Harness) -> None:
        """
        §53 + 2.5.1: возврат не затрагивает пользовательские анонсы/черновики.

        Справка открыта из «📂 Мои анонсы». Сам список — экран конструктора
        без персистентного контекста (`announcement_ui` очищается веткой
        конструктора до `render_help`), поэтому возврат идёт в главное меню:
        это безопасный фолбэк, а не потеря данных — черновики остаются.
        """
        await h.text("/start")
        await h.callback("my_announcements")
        await h.callback("help")
        deletes_before_back = len(h.deleted_ids())
        await h.callback(BACK_DATA)

        # новых удалений ровно одно — сообщение справки
        self.assertEqual(len(h.deleted_ids()), deletes_before_back + 1)
        self.assertEqual(len(h.bot.rich_records), 1)
        self.assertIn("Что хотите сделать?", h.screen_text(), "вернулись не в меню")
        # Список анонсов доступен по-прежнему: черновики не тронуты.
        await h.callback("my_announcements")
        self.assertIn("Мои анонсы", h.screen_text())

    @scenario_test
    async def test_other_sections_do_not_use_rich_message(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("manage")
        await h.callback("manage_places")
        await h.callback("main_menu")
        await h.callback("my_announcements")
        self.assertEqual(h.bot.rich_records, [])

    @scenario_test
    async def test_help_button_in_manage_section_is_regular(self, h: Harness) -> None:
        """Раздел «Управление» — обычный экран, не Rich Message."""
        await h.text("/start")
        await h.callback("manage")
        self.assertEqual(h.bot.rich_records, [])
        self.assertTrue(h.screen_text())


class HelpFallbackTests(unittest.TestCase):
    """Отказ Telegram: причина в лог, Rich Message обычным текстом не подменяется."""

    @scenario_test
    async def test_failed_rich_message_does_not_fake_plain_help(
        self, h: Harness
    ) -> None:
        """
        §54: если `sendRichMessage` отклонён, справка НЕ показывается через
        sendMessage «как будто это Rich Message». Пользователь видит честную
        плашку о недоступности, Rich Message не записан, message_id справки
        не появляется в user_data.
        """
        h.bot.errors_by_type["rich_message"] = BadRequest(
            "Bad Request: unsupported rich message"
        )
        await h.text("/start")
        await h.callback("help")

        self.assertEqual(h.bot.rich_records, [])
        self.assertNotIn("help_rich_message_id", h.user_data())
        self.assertIn(
            ui_texts.DEFAULTS["help.rich.unavailable"].split(".")[0],
            h.screen_text(),
        )
        labels = [label for label, _ in h.screen_buttons()]
        self.assertIn("📝 Создать анонс", labels)


if __name__ == "__main__":
    unittest.main()
