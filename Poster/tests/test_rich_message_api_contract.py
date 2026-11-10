# tests/test_rich_message_api_contract.py
"""
Контракт сериализации Rich Message с ОФИЦИАЛЬНОЙ схемой Telegram Bot API 10.1.

Источник истины — документация core.telegram.org/bots/api, разделы
`InputRichMessage` / `InputRichBlock`, а не внутренние классы адаптера.
Поэтому проверяется готовый JSON-словарь `build_help_rich_message().to_dict()`
по точным именам полей и значений; dataclass'ы используются только как
вход для unit-тестов валидации.

Что закрепляется по официальной схеме:

- `InputRichBlockSectionHeading`: `type = "heading"`, поля `text` и
  обязательный `size` (1–6, 1 — крупнейший); поля `heading` НЕТ;
- `InputRichBlockParagraph`: `type = "paragraph"`, `text`;
- `InputRichBlockList`: `type = "list"`, `items` — массив объектов
  `InputRichBlockListItem`, у каждого обязательный `blocks`;
- `InputRichBlockDivider`: `type = "divider"` без полей;
- `InputRichBlockDetails`: `type`, `summary`, `blocks`; `is_open` —
  Optional и передаётся ТОЛЬКО при True (отсутствие = свёрнуто);
- `InputRichMessage`: ровно один способ содержимого (справка — `blocks`).

Poster 2.5.4 §7 добавил второй Rich Message («О проекте») и `RichTextUrl`:
поле `text` у `heading`/`paragraph` и `summary` у `details` — это `RichText`
(строка, массив `RichText` или типизированный объект), а `RichTextUrl`
(`type: "url"`, поля `text` и `url`) даёт кликабельную подпись вместо
«голого» адреса. Контракт проверяется для ОБОИХ сообщений.

Реальная сеть, Telegram, `.env` и `post_bot.db` не используются.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from telegram import InputFile

from help_rich import build_help_rich_message
from rich_message import (
    InputRichBlockDetails,
    InputRichBlockList,
    InputRichBlockListItem,
    InputRichBlockParagraph,
    InputRichBlockSectionHeading,
    InputRichMessage,
    InputRichTextUrl,
    RichMessageError,
    _hoist_input_files,
    rich_text_plain,
    validate_rich_message,
)
from utils.rich_content import load_catalog

# Точный набор ключей блоков официальной схемы. Лишний/недостающий ключ —
# нарушение контракта, а не «мелкая деталь сериализации».
HEADING_KEYS = {"type", "text", "size"}
PARAGRAPH_KEYS = {"type", "text"}
LIST_KEYS = {"type", "items"}
LIST_ITEM_KEYS = {"blocks"}
DIVIDER_KEYS = {"type"}
DETAILS_KEYS = {"type", "summary", "blocks"}
# RichTextUrl: подпись (RichText) и адрес ссылки.
URL_KEYS = {"type", "text", "url"}
# RichTextBold: жирная фраза внутри абзаца (§14.6).
BOLD_KEYS = {"type", "text"}


def _payload() -> dict[str, Any]:
    """Готовый `rich_message` справки (как уходит в Bot API)."""
    payload = build_help_rich_message().to_dict()
    assert isinstance(payload, dict)
    return payload


def _blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = payload["blocks"]
    assert isinstance(blocks, list)
    return blocks


def _walk(block: dict[str, Any]):
    """Обходит блоки рекурсивно (details и элементы списков)."""
    yield block
    for child in block.get("blocks") or ():
        yield from _walk(child)
    for item in block.get("items") or ():
        for child in item.get("blocks") or ():
            yield from _walk(child)


def _all_blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for block in _blocks(payload):
        found.extend(_walk(block))
    return found


def _rich_text_objects(value: Any) -> list[dict[str, Any]]:
    """Типизированные объекты `RichText` (например `RichTextUrl`) внутри поля."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [obj for item in value for obj in _rich_text_objects(item)]
    return []


def _all_urls(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Все `RichTextUrl` сообщения (внутри `text` и `summary`)."""
    found: list[dict[str, Any]] = []
    for block in _all_blocks(payload):
        for field in ("text", "summary"):
            found.extend(
                obj
                for obj in _rich_text_objects(block.get(field))
                if obj.get("type") == "url"
            )
    return found


def _help_page():
    """Страница «Помощь» из каталога (единственный источник §15.1)."""
    return load_catalog().help


def _about_page():
    """Страница «О проекте» из каталога."""
    return load_catalog().about


def _plain(value: Any) -> str:
    """Плоский текст `RichText` из готового словаря Bot API."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_plain(item) for item in value)
    if isinstance(value, dict):
        return _plain(value.get("text", ""))
    return ""


# —— 1. Официальная схема готового запроса ————————————————————————————


class HelpRichOfficialSchemaTests(unittest.TestCase):
    """JSON справки соответствует Bot API 10.1 буквально по именам полей."""

    def setUp(self) -> None:
        self.payload = _payload()

    def test_payload_is_json_and_single_way(self) -> None:
        """
        Итоговый `rich_message` — JSON: файлы слайд-шоу выносятся отдельно.

        В `photo.media` лежат живые `InputFile` (их загружает
        `send_rich_message`), поэтому JSON-представление берётся ПОСЛЕ
        штатного hoisting — ровно в той форме, что уходит в Bot API
        (`attach://rich_media_N` вместо объекта).
        """
        self.assertEqual(list(self.payload), ["blocks"], "ровно один способ")
        files: list[tuple[str, InputFile]] = []
        hoisted = _hoist_input_files(self.payload, files)
        encoded = json.dumps(hoisted, ensure_ascii=False)
        self.assertEqual(json.loads(encoded), hoisted, "не JSON-структура")
        # Без медиа payload обязан быть JSON сразу — лишних объектов нет.
        if not files:
            self.assertEqual(json.loads(json.dumps(self.payload)), self.payload)

    def test_heading_is_official_heading_block(self) -> None:
        heading = _blocks(self.payload)[0]
        self.assertEqual(set(heading), HEADING_KEYS)
        self.assertEqual(heading["type"], "heading")
        self.assertEqual(heading["text"], _help_page().title)
        # size обязателен: 1 — самый крупный заголовок.
        self.assertEqual(heading["size"], 1)
        # Поля "heading" в схеме нет.
        self.assertNotIn("heading", heading)

    def test_divider_has_no_extra_fields(self) -> None:
        dividers = [b for b in _blocks(self.payload) if b["type"] == "divider"]
        self.assertEqual(len(dividers), 1, "в справке один разделитель")
        for divider in dividers:
            self.assertEqual(set(divider), DIVIDER_KEYS, "у divider нет полей")

    def test_details_omit_is_open_when_collapsed(self) -> None:
        details = [b for b in _blocks(self.payload) if b["type"] == "details"]
        self.assertEqual(len(details), len(_help_page().sections))
        for block in details:
            self.assertEqual(set(block), DETAILS_KEYS, "лишние поля в details")
            self.assertNotIn("is_open", block, "свёрнутость = отсутствие поля")
            self.assertTrue(block["summary"], "пустой summary")

    def test_table_and_quotes_are_official_objects(self) -> None:
        """
        В справке таблица подстановки и две Rich-цитаты (§13.3, §13.7).

        Раньше здесь проверялись элементы списка; утверждённый текст списков
        не содержит, поэтому тот же смысл — «вложенные блоки являются
        объектами официальной схемы, а не строками» — проверяется на
        таблице и цитатах.
        """
        tables = [b for b in _all_blocks(self.payload) if b["type"] == "table"]
        self.assertEqual(len(tables), 1, "в справке одна таблица")
        table = tables[0]
        self.assertEqual(set(table), {"type", "cells", "is_bordered", "is_compact"})
        self.assertIsInstance(table["cells"], list)
        for row in table["cells"]:
            self.assertIsInstance(row, list)
            self.assertTrue(row)
            for cell in row:
                self.assertIsInstance(cell, dict, "ячейка — объект, не строка")
                self.assertIn("align", cell)
                self.assertIn("valign", cell)

        quotes = [b for b in _all_blocks(self.payload) if b["type"] == "blockquote"]
        self.assertEqual(len(quotes), 2, "в сравнении две отдельные цитаты (§13.7)")
        for quote in quotes:
            self.assertEqual(set(quote), {"type", "blocks"})
            self.assertTrue(quote["blocks"], "цитата без текста")
            self.assertEqual(quote["blocks"][0]["type"], "paragraph")

    def test_paragraphs_use_text_field(self) -> None:
        paragraphs = [b for b in _all_blocks(self.payload) if b["type"] == "paragraph"]
        self.assertTrue(paragraphs)
        for block in paragraphs:
            self.assertEqual(set(block), PARAGRAPH_KEYS)
            # `text` — RichText: строка ИЛИ массив частей (жирный, ссылка).
            self.assertTrue(_plain(block["text"]).strip(), "пустой текст абзаца")

    def test_no_legacy_type_or_field_names_anywhere(self) -> None:
        """Подстраховка от откатов: старых имён в запросе быть не должно."""
        for block in _all_blocks(self.payload):
            self.assertNotEqual(
                block["type"], "section_heading", "устаревший тип заголовка"
            )
            self.assertNotIn("heading", block, "устаревшее поле заголовка")
            if block["type"] == "list":
                for item in block["items"]:
                    self.assertNotIsInstance(item, str, "items — не строки")

    def test_help_specific_expectations_survive(self) -> None:
        """Семантика справки не потеряна при смене схемы (§51–52)."""
        blocks = _blocks(self.payload)
        page = _help_page()
        self.assertEqual(blocks[0]["text"], page.title)
        texts = [_plain(b["text"]) for b in blocks if b["type"] == "paragraph"]
        self.assertIn(page.hint, texts)
        sections = [
            rich_text_plain(b["summary"]) for b in blocks if b["type"] == "details"
        ]
        self.assertEqual(sections, [section.title for section in page.sections])
        self.assertEqual(len(sections), 9, "§13: ровно девять разделов")

    def test_sections_and_table_are_readable_text(self) -> None:
        """§13.3: таблица подстановки читается как текст, а не как заглушка."""
        table = next(b for b in _all_blocks(self.payload) if b["type"] == "table")
        rows = [
            [_plain(cell.get("text", "")) for cell in row] for row in table["cells"]
        ]
        self.assertEqual(rows[0], ["В шаблоне", "В анонсе"])
        self.assertIn(["[дата]", "9 ноября"], rows)
        details = [b for b in _blocks(self.payload) if b["type"] == "details"]
        summary = [rich_text_plain(b["summary"]) for b in details]
        self.assertIn("Шаблоны и кирпичики", summary)


# —— 2. «О проекте» и RichTextUrl (Poster 2.5.4 §7) ——————————————————


class AboutRichOfficialSchemaTests(unittest.TestCase):
    """
    Второй Rich Message проверяется по той же официальной схеме.

    Ключевое отличие от справки — ссылки: `text`/`summary` остаются
    `RichText`, а `RichTextUrl` передаётся объектом `{type, text, url}`, а не
    строкой. Тест закрепляет имена полей и то, что в запросе нет ничего
    лишнего (никаких `href`/`label`/`caption`).
    """

    def setUp(self) -> None:
        from about_rich import build_about_rich_message

        self.payload = build_about_rich_message().to_dict()

    def test_payload_is_json_and_single_way(self) -> None:
        self.assertEqual(list(self.payload), ["blocks"])
        encoded = json.dumps(self.payload, ensure_ascii=False)
        self.assertEqual(json.loads(encoded), self.payload)

    def test_blocks_use_only_official_shapes(self) -> None:
        allowed = {
            "heading": HEADING_KEYS,
            "paragraph": PARAGRAPH_KEYS,
            "list": LIST_KEYS,
            "divider": DIVIDER_KEYS,
            "details": DETAILS_KEYS,
        }
        for block in _all_blocks(self.payload):
            self.assertIn(block["type"], allowed, "неизвестный тип блока")
            self.assertEqual(set(block), allowed[block["type"]], "лишние поля в блоке")
            if block["type"] == "list":
                for item in block["items"]:
                    self.assertEqual(set(item), LIST_ITEM_KEYS)

    def test_url_objects_have_official_fields(self) -> None:
        urls = _all_urls(self.payload)
        self.assertTrue(urls, "в «О проекте» нет RichTextUrl")
        for item in urls:
            self.assertEqual(set(item), URL_KEYS)
            self.assertIsInstance(item["url"], str)
            # Подпись — снова RichText: строка.
            self.assertIsInstance(item["text"], str)
            self.assertTrue(item["text"].strip())

    def test_rich_text_array_is_a_list_of_strings_or_urls(self) -> None:
        """Массив `RichText` (фраза со ссылкой/жирным) — плоский и непустой."""
        for block in _all_blocks(self.payload):
            for field in ("text", "summary"):
                value = block.get(field)
                if not isinstance(value, list):
                    continue
                self.assertTrue(value, "пустой массив RichText")
                for part in value:
                    self.assertIsInstance(part, (str, dict))
                    if isinstance(part, dict):
                        # Жирная фраза (§14.6) — тоже допустимая часть RichText.
                        self.assertIn(set(part), (URL_KEYS, BOLD_KEYS))

    def test_semantics_survive_serialization(self) -> None:
        blocks = _blocks(self.payload)
        page = _about_page()
        self.assertEqual(blocks[0]["text"], page.title)
        self.assertEqual(
            [rich_text_plain(b["summary"]) for b in blocks if b["type"] == "details"],
            [section.title for section in page.sections],
        )
        self.assertEqual(len(page.sections), 6, "§14: ровно шесть разделов")


class RichTextUrlFixtureTests(unittest.TestCase):
    """RichTextUrl как вход адаптера: сериализация и проверка до сети."""

    def test_url_serializes_by_schema(self) -> None:
        block = InputRichBlockParagraph(
            text=InputRichTextUrl(text="GitHub", url="https://github.com/x")
        )
        self.assertEqual(
            block.to_dict(),
            {
                "type": "paragraph",
                "text": {
                    "type": "url",
                    "text": "GitHub",
                    "url": "https://github.com/x",
                },
            },
        )
        validate_rich_message(InputRichMessage(blocks=(block,)))

    def test_string_paragraph_stays_string(self) -> None:
        """Обычный текст не превращается в объект: запрос не растёт."""
        self.assertEqual(
            InputRichBlockParagraph(text="привет").to_dict(),
            {"type": "paragraph", "text": "привет"},
        )

    def test_array_of_parts_serializes_in_order(self) -> None:
        block = InputRichBlockParagraph(
            text=(
                "Код: ",
                InputRichTextUrl(text="репозиторий", url="https://github.com/x"),
                " — открыт.",
            )
        )
        payload = block.to_dict()
        self.assertIsInstance(payload["text"], list)
        self.assertEqual(
            [
                part if isinstance(part, str) else part["type"]
                for part in payload["text"]
            ],
            ["Код: ", "url", " — открыт."],
        )

    def test_url_requires_http_and_label(self) -> None:
        for url in ("", "ftp://example.com", "example.com", " t.me/x"):
            with self.subTest(url=url):
                with self.assertRaises(RichMessageError):
                    validate_rich_message(
                        InputRichMessage(
                            blocks=(
                                InputRichBlockParagraph(
                                    text=InputRichTextUrl(text="x", url=url)
                                ),
                            )
                        )
                    )

    def test_url_requires_label(self) -> None:
        with self.assertRaises(RichMessageError):
            validate_rich_message(
                InputRichMessage(
                    blocks=(
                        InputRichBlockParagraph(
                            text=InputRichTextUrl(text="  ", url="https://e.com")
                        ),
                    )
                )
            )

    def test_empty_array_is_rejected(self) -> None:
        with self.assertRaises(RichMessageError):
            validate_rich_message(
                InputRichMessage(blocks=(InputRichBlockParagraph(text=()),))
            )

    def test_summary_accepts_rich_text(self) -> None:
        """`summary` details — тоже RichText: строка и ссылка равноправны."""
        block = InputRichBlockDetails(
            summary=InputRichTextUrl(text="Ссылка", url="https://e.com"),
            blocks=(InputRichBlockParagraph(text="текст"),),
        )
        validate_rich_message(InputRichMessage(blocks=(block,)))
        self.assertEqual(
            block.to_dict()["summary"],
            {"type": "url", "text": "Ссылка", "url": "https://e.com"},
        )


# —— 3. Валидация: official-схема отвергает неверные значения ————————


class OfficialSchemaValidationTests(unittest.TestCase):
    """Невалидные по схеме значения ловятся ДО сети как RichMessageError."""

    def _validate_blocks(self, *blocks: Any) -> None:
        validate_rich_message(InputRichMessage(blocks=blocks))

    def test_heading_size_bounds(self) -> None:
        for size in (0, 7, -1, 100):
            with self.subTest(size=size):
                with self.assertRaises(RichMessageError):
                    self._validate_blocks(
                        InputRichBlockSectionHeading(text="Заголовок", size=size)
                    )

    def test_heading_size_must_be_int(self) -> None:
        for size in ("2", 2.0, None, True):
            with self.subTest(size=size):
                with self.assertRaises(RichMessageError):
                    self._validate_blocks(
                        InputRichBlockSectionHeading(text="Заголовок", size=size)  # type: ignore[arg-type]
                    )

    def test_heading_size_valid_range_is_accepted(self) -> None:
        for size in range(1, 7):
            with self.subTest(size=size):
                self._validate_blocks(
                    InputRichBlockSectionHeading(text="Заголовок", size=size)
                )

    def test_heading_size_default_is_1(self) -> None:
        self.assertEqual(
            InputRichBlockSectionHeading(text="Заголовок").to_dict(),
            {"type": "heading", "text": "Заголовок", "size": 1},
        )

    def test_empty_heading_text_is_rejected(self) -> None:
        with self.assertRaises(RichMessageError):
            self._validate_blocks(InputRichBlockSectionHeading(text="   "))

    def test_list_with_string_items_is_rejected(self) -> None:
        """По схеме 10.1 items — объекты, а не строки."""
        with self.assertRaises(RichMessageError):
            self._validate_blocks(
                InputRichBlockList(items=("строка вместо объекта",))  # type: ignore[arg-type]
            )

    def test_list_with_empty_items_is_rejected(self) -> None:
        with self.assertRaises(RichMessageError):
            self._validate_blocks(InputRichBlockList(items=()))

    def test_list_item_with_empty_blocks_is_rejected(self) -> None:
        with self.assertRaises(RichMessageError):
            self._validate_blocks(
                InputRichBlockList(items=(InputRichBlockListItem(blocks=()),))
            )

    def test_list_item_with_invalid_child_block_is_rejected(self) -> None:
        with self.assertRaises(RichMessageError):
            self._validate_blocks(
                InputRichBlockList(
                    items=(
                        InputRichBlockListItem(
                            blocks=(InputRichBlockParagraph(text=""),)
                        ),
                    )
                )
            )

    def test_valid_list_item_serializes_as_blocks(self) -> None:
        item = InputRichBlockListItem(
            blocks=(InputRichBlockParagraph(text="/start — меню"),)
        )
        self.assertEqual(
            InputRichBlockList(items=(item,)).to_dict(),
            {
                "type": "list",
                "items": [{"blocks": [{"type": "paragraph", "text": "/start — меню"}]}],
            },
        )

    def test_is_open_false_is_not_serialized(self) -> None:
        block = InputRichBlockDetails(summary="Раздел", blocks=(), is_open=False)
        self.assertNotIn("is_open", block.to_dict())

    def test_is_open_none_is_not_serialized(self) -> None:
        block = InputRichBlockDetails(summary="Раздел", blocks=())
        self.assertNotIn("is_open", block.to_dict())

    def test_is_open_true_is_serialized(self) -> None:
        block = InputRichBlockDetails(summary="Раздел", blocks=(), is_open=True)
        self.assertIs(block.to_dict()["is_open"], True)

    def test_is_open_must_be_bool(self) -> None:
        with self.assertRaises(RichMessageError):
            self._validate_blocks(
                InputRichBlockDetails(
                    summary="Раздел",
                    blocks=(InputRichBlockParagraph(text="текст"),),
                    is_open="yes",  # type: ignore[arg-type]
                )
            )


if __name__ == "__main__":
    unittest.main()
