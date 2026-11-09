# tests/test_stage26_d1_rich_blocks.py
"""
Этап D1 (§15.2–15.5): новые блоки Rich Message и загрузка медиа слайд-шоу.

Что закрепляется:

(а) ТОЧНАЯ сериализация по официальной схеме Bot API 10.4:
    - `table` — `cells` из ячеек с обязательными `align`/`valign`,
      необязательные `is_bordered`/`is_striped`/`is_compact`/`caption`;
    - `blockquote` — `blocks` и необязательный `credit` (не
      `expandable_blockquote` и не `pullquote`);
    - `slideshow` — `blocks` из фото-блоков (`photo` = `InputMediaPhoto`,
      подпись `RichBlockCaption`) и необязательный `caption`.
(б) Валидатор ДО сети отвергает каждое битое правило новых блоков.
(в) Хоистинг `InputFile`: PTB разбирает только верхний уровень
    `api_kwargs`, поэтому файл из слайд-шоу поднимается в `api_kwargs`
    (`rich_media_0`, …), а в payload остаётся `attach://…`-ссылка; payload
    без файлов не меняется; отсутствие медиа не мешает отправке сообщения
    без слайд-шоу.
(г) Контракт `send_rich_message` (возвращаемый тип, `RichMessageError`,
    отсутствие сети при невалидной структуре) сохранён.

Реальная сеть, Telegram, `.env` и `post_bot.db` не используются.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any, cast
from unittest import mock

from telegram import Chat, InputFile, InputMediaPhoto, Message
from telegram.request._requestdata import RequestData
from telegram.request._requestparameter import RequestParameter

from rich_message import (
    MAX_DETAILS_DEPTH,
    RICH_MEDIA_PREFIX,
    InputRichBlockBlockQuotation,
    InputRichBlockDetails,
    InputRichBlockDivider,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichBlockSlideshow,
    InputRichBlockTable,
    InputRichBlockTableCell,
    InputRichMessage,
    InputRichTextUrl,
    RichBlockCaption,
    RichMessageError,
    _hoist_input_files,
    send_rich_message,
    validate_rich_message,
)
from tests.integration_harness import FIXTURE_DATE

# Точные наборы ключей официальной схемы: лишний/недостающий ключ — ошибка.
CELL_KEYS = {"align", "valign"}
PHOTO_KEYS = {"type", "photo"}
SLIDESHOW_KEYS = {"type", "blocks"}
QUOTE_KEYS = {"type", "blocks"}


def _message(message_id: int = 800) -> Message:
    return Message(
        message_id=message_id,
        date=FIXTURE_DATE,
        chat=Chat(id=777, type="private"),
        text=None,
    )


class StubBot:
    """Минимальный бот: запоминает `api_kwargs` последнего вызова."""

    def __init__(self, answer: Any = None) -> None:
        self.answer = answer
        self.endpoint: str | None = None
        self.api_kwargs: dict[str, Any] | None = None

    @property
    def sent(self) -> dict[str, Any]:
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
        return self.answer


def _run(coro):
    return asyncio.run(coro)


def _cell(**extra: Any) -> InputRichBlockTableCell:
    """Ячейка с обязательными выравниваниями по умолчанию."""
    extra.setdefault("align", "left")
    extra.setdefault("valign", "top")
    return InputRichBlockTableCell(**extra)


def _photo(media: Any = b"png-bytes") -> InputRichBlockPhoto:
    return InputRichBlockPhoto(photo=InputMediaPhoto(media, filename="frame.png"))


# —— 1. Точная сериализация —————————————————————————————————————————


class TableSerializationTests(unittest.TestCase):
    """`table` → `InputRichBlockTable` с `cells`, флагами и `caption`."""

    def test_minimal_cell_has_required_alignments(self) -> None:
        self.assertEqual(_cell().to_dict(), {"align": "left", "valign": "top"})

    def test_full_cell_serializes_all_optional_fields(self) -> None:
        cell = InputRichBlockTableCell(
            align="center",
            valign="middle",
            text=InputRichTextUrl(text="Открыть", url="https://example.org"),
            is_header=True,
            colspan=2,
            rowspan=3,
        )
        self.assertEqual(
            cell.to_dict(),
            {
                "align": "center",
                "valign": "middle",
                "text": {
                    "type": "url",
                    "text": "Открыть",
                    "url": "https://example.org",
                },
                "is_header": True,
                "colspan": 2,
                "rowspan": 3,
            },
        )

    def test_cell_without_text_does_not_send_text(self) -> None:
        self.assertNotIn("text", _cell().to_dict())
        self.assertNotIn("is_header", _cell().to_dict())

    def test_table_serialization(self) -> None:
        table = InputRichBlockTable(
            cells=(
                (_cell(text="Шаг", is_header=True), _cell(text="Действие")),
                (_cell(text="1", align="right"), _cell(text="Создать анонс")),
            ),
            is_bordered=True,
            is_striped=False,
            is_compact=True,
            caption="Порядок действий",
        )
        payload = table.to_dict()
        self.assertEqual(
            set(payload),
            {"type", "cells", "is_bordered", "is_striped", "is_compact", "caption"},
        )
        self.assertEqual(payload["type"], "table")
        self.assertEqual(len(payload["cells"]), 2)
        self.assertEqual(set(payload["cells"][0][0]), CELL_KEYS | {"text", "is_header"})
        self.assertEqual(payload["cells"][1][0]["align"], "right")
        self.assertEqual(payload["cells"][0][0]["text"], "Шаг")
        self.assertTrue(payload["is_bordered"])
        self.assertFalse(payload["is_striped"])
        self.assertTrue(payload["is_compact"])
        self.assertEqual(payload["caption"], "Порядок действий")

    def test_table_optional_fields_are_omitted(self) -> None:
        payload = InputRichBlockTable(cells=((_cell(),),)).to_dict()
        self.assertEqual(set(payload), {"type", "cells"})
        json.dumps(payload)  # сериализуется без медиа

    def test_table_inside_message(self) -> None:
        message = InputRichMessage(blocks=(InputRichBlockTable(cells=((_cell(),),)),))
        self.assertEqual(
            message.to_dict(),
            {"blocks": [InputRichBlockTable(cells=((_cell(),),)).to_dict()]},
        )


class BlockQuoteSerializationTests(unittest.TestCase):
    """`blockquote` → `InputRichBlockBlockQuotation`."""

    def test_quote_with_blocks_and_credit(self) -> None:
        quote = InputRichBlockBlockQuotation(
            blocks=(InputRichBlockParagraph(text="Цитата"),),
            credit="Автор",
        )
        self.assertEqual(
            quote.to_dict(),
            {
                "type": "blockquote",
                "blocks": [{"type": "paragraph", "text": "Цитата"}],
                "credit": "Автор",
            },
        )

    def test_quote_without_credit_has_no_credit_key(self) -> None:
        payload = InputRichBlockBlockQuotation(
            blocks=(InputRichBlockParagraph(text="Цитата"),)
        ).to_dict()
        self.assertEqual(set(payload), QUOTE_KEYS)

    def test_quote_nested_in_details_serializes_recursively(self) -> None:
        message = InputRichMessage(
            blocks=(
                InputRichBlockDetails(
                    summary="Раздел",
                    blocks=(
                        InputRichBlockBlockQuotation(
                            blocks=(InputRichBlockParagraph(text="Внутри"),)
                        ),
                    ),
                ),
            )
        )
        quote = message.to_dict()["blocks"][0]["blocks"][0]
        self.assertEqual(quote["type"], "blockquote")
        self.assertEqual(quote["blocks"][0]["text"], "Внутри")

    def test_quote_serializes_rich_text_credit(self) -> None:
        quote = InputRichBlockBlockQuotation(
            blocks=(InputRichBlockParagraph(text="Текст"),),
            credit=InputRichTextUrl(text="Источник", url="https://example.org"),
        )
        self.assertEqual(
            quote.to_dict()["credit"],
            {"type": "url", "text": "Источник", "url": "https://example.org"},
        )


class SlideshowSerializationTests(unittest.TestCase):
    """`slideshow` → `InputRichBlockSlideshow` из фото-блоков."""

    def test_photo_serialization_keeps_input_media(self) -> None:
        photo = _photo("FILE_ID_1")
        payload = photo.to_dict()
        self.assertEqual(set(payload), PHOTO_KEYS)
        self.assertEqual(payload["type"], "photo")
        self.assertEqual(payload["photo"], {"media": "FILE_ID_1", "type": "photo"})

    def test_photo_caption_serialization(self) -> None:
        photo = InputRichBlockPhoto(
            photo=InputMediaPhoto("FILE_ID_1"),
            caption=RichBlockCaption(text="Подпись", credit="Автор"),
        )
        self.assertEqual(
            photo.to_dict()["caption"],
            {"text": "Подпись", "credit": "Автор"},
        )

    def test_slideshow_two_frames_in_order(self) -> None:
        slideshow = InputRichBlockSlideshow(
            blocks=(_photo("FILE_1"), _photo("FILE_2")),
            caption=RichBlockCaption(text="Как это выглядит"),
        )
        payload = slideshow.to_dict()
        self.assertEqual(set(payload), SLIDESHOW_KEYS | {"caption"})
        self.assertEqual(payload["type"], "slideshow")
        self.assertEqual(
            [frame["photo"]["media"] for frame in payload["blocks"]],
            ["FILE_1", "FILE_2"],
        )
        self.assertEqual(payload["caption"], {"text": "Как это выглядит"})

    def test_slideshow_without_caption_has_no_caption_key(self) -> None:
        payload = InputRichBlockSlideshow(blocks=(_photo("FILE_1"),)).to_dict()
        self.assertEqual(set(payload), SLIDESHOW_KEYS)

    def test_slideshow_inside_details_is_json_serializable(self) -> None:
        """Слайд-шоу — настоящий блок: без InputFile payload сериализуется."""
        message = InputRichMessage(
            blocks=(
                InputRichBlockDetails(
                    summary="Редактирование и публикация",
                    blocks=(InputRichBlockSlideshow(blocks=(_photo("FILE_1"),)),),
                ),
            )
        )
        json.dumps(message.to_dict())

    def test_slideshow_block_type_is_known(self) -> None:
        validate_rich_message(
            InputRichMessage(blocks=(InputRichBlockSlideshow(blocks=(_photo(),)),))
        )

    def test_new_blocks_keep_old_contract_intact(self) -> None:
        """Старые блоки сериализуются по-прежнему (контракт не сломан)."""
        self.assertEqual(
            InputRichBlockDivider().to_dict(),
            {"type": "divider"},
        )
        self.assertEqual(
            InputRichBlockParagraph(text="x").to_dict(),
            {"type": "paragraph", "text": "x"},
        )


# —— 2. Валидация новых блоков ——————————————————————————————————————


class NewBlockValidationTests(unittest.TestCase):
    """Каждое битое правило новых блоков отвергается ДО сети."""

    def _reject(self, block: Any) -> None:
        with self.assertRaises(RichMessageError):
            validate_rich_message(InputRichMessage(blocks=(block,)))

    # таблица

    def test_empty_table_is_rejected(self) -> None:
        self._reject(InputRichBlockTable(cells=()))

    def test_empty_table_row_is_rejected(self) -> None:
        self._reject(InputRichBlockTable(cells=((_cell(),), ())))

    def test_table_row_must_be_sequence(self) -> None:
        self._reject(InputRichBlockTable(cells=cast(Any, (("a", "b"),))))

    def test_table_cells_must_be_sequence(self) -> None:
        self._reject(InputRichBlockTable(cells=cast(Any, ("abc",))))

    def test_table_cell_must_be_cell_object(self) -> None:
        self._reject(InputRichBlockTable(cells=cast(Any, (("ячейка",),))))

    def test_table_cell_requires_align(self) -> None:
        self._reject(InputRichBlockTable(cells=((_cell(align=None),),)))

    def test_table_cell_align_enum(self) -> None:
        self._reject(InputRichBlockTable(cells=((_cell(align="justify"),),)))

    def test_table_cell_requires_valign(self) -> None:
        self._reject(InputRichBlockTable(cells=((_cell(valign=None),),)))

    def test_table_cell_valign_enum(self) -> None:
        self._reject(InputRichBlockTable(cells=((_cell(valign="baseline"),),)))

    def test_table_cell_text_must_be_rich_text(self) -> None:
        self._reject(InputRichBlockTable(cells=((_cell(text="  "),),)))
        self._reject(InputRichBlockTable(cells=((_cell(text=5),),)))

    def test_table_cell_is_header_must_be_bool(self) -> None:
        self._reject(InputRichBlockTable(cells=((_cell(is_header="yes"),),)))

    def test_table_cell_spans_must_be_positive_ints(self) -> None:
        for value in (0, -1, True, "2"):
            with self.subTest(value=value):
                self._reject(InputRichBlockTable(cells=((_cell(colspan=value),),)))
                self._reject(InputRichBlockTable(cells=((_cell(rowspan=value),),)))

    def test_table_flags_must_be_bool(self) -> None:
        for name in ("is_bordered", "is_striped", "is_compact"):
            with self.subTest(name=name):
                flags = cast(Any, {name: "да"})
                table = InputRichBlockTable(cells=((_cell(),),), **flags)
                self._reject(table)

    def test_table_caption_must_be_rich_text(self) -> None:
        self._reject(InputRichBlockTable(cells=((_cell(),),), caption=""))
        self._reject(InputRichBlockTable(cells=((_cell(),),), caption=cast(Any, 1)))

    def test_valid_table_passes(self) -> None:
        validate_rich_message(
            InputRichMessage(
                blocks=(
                    InputRichBlockTable(
                        cells=(
                            (
                                InputRichBlockTableCell(
                                    align="center",
                                    valign="bottom",
                                    text="Итог",
                                    is_header=True,
                                    colspan=2,
                                    rowspan=1,
                                ),
                            ),
                        ),
                        is_bordered=True,
                        caption="Таблица",
                    ),
                )
            )
        )

    # цитата

    def test_empty_quote_is_rejected(self) -> None:
        self._reject(InputRichBlockBlockQuotation(blocks=()))

    def test_quote_blocks_must_be_list(self) -> None:
        self._reject(InputRichBlockBlockQuotation(blocks=cast(Any, "цитата")))

    def test_quote_blocks_validated_recursively(self) -> None:
        self._reject(
            InputRichBlockBlockQuotation(blocks=(InputRichBlockParagraph(text=""),))
        )

    def test_quote_credit_must_be_rich_text(self) -> None:
        self._reject(
            InputRichBlockBlockQuotation(
                blocks=(InputRichBlockParagraph(text="Цитата"),), credit=" "
            )
        )

    def test_deep_details_inside_quote_is_rejected(self) -> None:
        """Цитата — контейнер: глубина `details` считается и внутри неё."""
        nested: Any = InputRichBlockParagraph(text="дно")
        for level in range(MAX_DETAILS_DEPTH + 1):
            nested = InputRichBlockDetails(summary=f"ур.{level}", blocks=(nested,))
        self._reject(InputRichBlockBlockQuotation(blocks=(nested,)))
        with self.assertRaises(RichMessageError):
            InputRichMessage(
                blocks=(InputRichBlockBlockQuotation(blocks=(nested,)),)
            ).to_dict()

    def test_valid_quote_passes(self) -> None:
        validate_rich_message(
            InputRichMessage(
                blocks=(
                    InputRichBlockBlockQuotation(
                        blocks=(InputRichBlockParagraph(text="Цитата"),),
                        credit=InputRichTextUrl(
                            text="Автор", url="https://example.org"
                        ),
                    ),
                )
            )
        )

    # слайд-шоу

    def test_empty_slideshow_is_rejected(self) -> None:
        self._reject(InputRichBlockSlideshow(blocks=()))

    def test_slideshow_blocks_must_be_list(self) -> None:
        self._reject(InputRichBlockSlideshow(blocks=cast(Any, "photo")))

    def test_slideshow_accepts_only_photo_blocks(self) -> None:
        self._reject(
            InputRichBlockSlideshow(
                blocks=cast(Any, (InputRichBlockParagraph(text="x"),))
            )
        )
        self._reject(InputRichBlockSlideshow(blocks=cast(Any, ("FILE_ID",))))

    def test_photo_requires_input_media_photo(self) -> None:
        self._reject(InputRichBlockPhoto(photo=cast(Any, "FILE_ID")))

    def test_photo_media_must_be_non_empty(self) -> None:
        self._reject(InputRichBlockPhoto(photo=InputMediaPhoto("  ")))

    def test_photo_caption_must_be_valid(self) -> None:
        self._reject(
            InputRichBlockPhoto(
                photo=InputMediaPhoto("FILE_ID"),
                caption=RichBlockCaption(text=""),
            )
        )
        self._reject(
            InputRichBlockPhoto(
                photo=InputMediaPhoto("FILE_ID"),
                caption=cast(Any, "подпись"),
            )
        )

    def test_slideshow_caption_must_be_valid(self) -> None:
        self._reject(
            InputRichBlockSlideshow(
                blocks=(_photo("FILE_1"),), caption=cast(Any, "подпись")
            )
        )

    def test_valid_slideshow_with_input_file_passes(self) -> None:
        validate_rich_message(
            InputRichMessage(
                blocks=(
                    InputRichBlockSlideshow(
                        blocks=(_photo(b"png"), _photo("FILE_2")),
                        caption=RichBlockCaption(text="Кадры"),
                    ),
                )
            )
        )

    def test_new_blocks_are_standalone_message_blocks(self) -> None:
        """Таблица, цитата и слайд-шоу — самостоятельные блоки сообщения."""
        validate_rich_message(
            InputRichMessage(
                blocks=(
                    InputRichBlockTable(cells=((_cell(),),)),
                    InputRichBlockBlockQuotation(
                        blocks=(InputRichBlockParagraph(text="x"),)
                    ),
                    InputRichBlockSlideshow(blocks=(_photo(),)),
                )
            )
        )


# —— 3. Хоистинг InputFile и отправка ———————————————————————————————


class HoistTests(unittest.TestCase):
    """Файл слайд-шоу поднимается в `api_kwargs`, payload получает attach://."""

    def test_nested_input_file_is_hoisted(self) -> None:
        file = InputFile(b"png", filename="frame.png")
        payload = InputRichBlockSlideshow(
            blocks=(InputRichBlockPhoto(photo=InputMediaPhoto(file)),)
        ).to_dict()

        files: list[tuple[str, InputFile]] = []
        hoisted = _hoist_input_files(payload, files)

        self.assertEqual(len(files), 1)
        name, hoisted_file = files[0]
        self.assertEqual(name, f"{RICH_MEDIA_PREFIX}0")
        self.assertIs(hoisted_file, file)
        media = hoisted["blocks"][0]["photo"]["media"]
        self.assertTrue(media.startswith("attach://"))
        json.dumps(hoisted)  # InputFile больше не мешает сериализации
        # исходный payload не изменён
        self.assertIs(payload["blocks"][0]["photo"]["media"], file)

    def test_two_files_get_distinct_names(self) -> None:
        payload = InputRichBlockSlideshow(
            blocks=(
                InputRichBlockPhoto(photo=InputMediaPhoto(b"one")),
                InputRichBlockPhoto(photo=InputMediaPhoto(b"two")),
            )
        ).to_dict()
        files: list[tuple[str, InputFile]] = []
        hoisted = _hoist_input_files(payload, files)
        self.assertEqual([name for name, _ in files], ["rich_media_0", "rich_media_1"])
        media = [frame["photo"]["media"] for frame in hoisted["blocks"]]
        self.assertNotEqual(media[0], media[1])
        self.assertTrue(all(item.startswith("attach://") for item in media))

    def test_payload_without_files_is_unchanged(self) -> None:
        payload = InputRichMessage(
            blocks=(
                InputRichBlockParagraph(text="текст"),
                InputRichBlockTable(cells=((_cell(text="a"),),)),
                InputRichBlockSlideshow(blocks=(_photo("FILE_ID"),)),
            )
        ).to_dict()
        files: list[tuple[str, InputFile]] = []
        self.assertEqual(_hoist_input_files(payload, files), payload)
        self.assertEqual(files, [])

    def test_file_without_attach_name_uses_parameter_name(self) -> None:
        """
        `InputFile` без `attach=True`: PTB возьмёт имя параметра.

        `InputMediaPhoto` обычно ставит `attach=True`, но готовый `InputFile`
        он не пересобирает. Тогда `attach_name` пуст, и ссылка обязана
        совпасть с именем параметра (`rich_media_N`), иначе PTB положит файл
        в multipart, а Telegram не найдёт его по `attach://`.
        """
        file = InputFile(b"png", filename="frame.png")  # attach_name is None
        payload = InputRichBlockSlideshow(
            blocks=(InputRichBlockPhoto(photo=InputMediaPhoto(file)),)
        ).to_dict()

        files: list[tuple[str, InputFile]] = []
        hoisted = _hoist_input_files(payload, files)
        self.assertEqual(files[0][0], "rich_media_0")
        self.assertEqual(
            hoisted["blocks"][0]["photo"]["media"], "attach://rich_media_0"
        )

        request_data = RequestData(
            [RequestParameter.from_input("rich_media_0", files[0][1])]
        )
        self.assertIn("rich_media_0", request_data.multipart_data)


class SendRichMessageMediaTests(unittest.TestCase):
    """`send_rich_message`: файлы доезжают до штатного механизма PTB."""

    def test_send_hoists_file_into_api_kwargs(self) -> None:
        bot = StubBot(answer=_message(801))
        file = InputFile(b"png-bytes", filename="help_forward.png")
        slideshow = InputRichBlockSlideshow(
            blocks=(InputRichBlockPhoto(photo=InputMediaPhoto(file)),)
        )
        result = _run(
            send_rich_message(bot, 777, InputRichMessage(blocks=(slideshow,)))
        )

        self.assertIsInstance(result, Message)
        self.assertEqual(bot.endpoint, "sendRichMessage")
        names = [key for key in bot.sent if key.startswith(RICH_MEDIA_PREFIX)]
        self.assertEqual(names, ["rich_media_0"])
        self.assertIs(bot.sent["rich_media_0"], file)
        media = bot.sent["rich_message"]["blocks"][0]["blocks"][0]["photo"]["media"]
        self.assertTrue(media.startswith("attach://"))

    def test_send_without_files_keeps_previous_payload(self) -> None:
        bot = StubBot(answer=_message())
        _run(
            send_rich_message(
                bot,
                777,
                InputRichMessage(blocks=(InputRichBlockDivider(),)),
            )
        )
        self.assertEqual(bot.sent["rich_message"], {"blocks": [{"type": "divider"}]})
        self.assertEqual(
            sorted(bot.sent),
            ["chat_id", "rich_message"],
            "payload без медиа не должен получать лишние параметры",
        )

    def test_reply_markup_survives_hoisting(self) -> None:
        bot = StubBot(answer=_message())
        _run(
            send_rich_message(
                bot,
                777,
                InputRichMessage(
                    blocks=(
                        InputRichBlockSlideshow(
                            blocks=(_photo(b"png"),),
                        ),
                    )
                ),
                reply_markup="MARKUP",
            )
        )
        self.assertEqual(bot.sent["reply_markup"], "MARKUP")

    def test_ptb_request_data_sees_multipart_upload(self) -> None:
        """
        Ключевой тест: штатный `RequestParameter` PTB строит multipart.

        Раньше `InputFile` внутри `rich_message` давал `TypeError` в
        `json.dumps`, то есть вызов падал ДО сети. После хоистинга PTB видит
        файл и кладёт его в отдельную multipart-часть.
        """
        bot = StubBot(answer=_message())
        _run(
            send_rich_message(
                bot,
                777,
                InputRichMessage(
                    blocks=(
                        InputRichBlockSlideshow(
                            blocks=(
                                _photo(b"first"),
                                _photo(b"second"),
                            )
                        ),
                    )
                ),
            )
        )
        request_data = RequestData(
            [RequestParameter.from_input(key, value) for key, value in bot.sent.items()]
        )
        self.assertTrue(request_data.contains_files)
        self.assertEqual(
            sorted(request_data.multipart_data),
            sorted(file.attach_name for file in self._files(bot)),
        )
        # rich_message уходит обычным JSON-параметром и содержит attach://
        encoded = json.loads(request_data.json_parameters["rich_message"])
        media = [frame["photo"]["media"] for frame in encoded["blocks"][0]["blocks"]]
        self.assertTrue(all(item.startswith("attach://") for item in media))

    @staticmethod
    def _files(bot: StubBot) -> list[InputFile]:
        return [
            value
            for key, value in bot.sent.items()
            if key.startswith(RICH_MEDIA_PREFIX)
        ]

    def test_invalid_structure_with_media_never_reaches_network(self) -> None:
        bot = StubBot(answer=_message())
        broken = InputRichMessage(
            blocks=(
                InputRichBlockSlideshow(
                    blocks=(InputRichBlockPhoto(photo=InputMediaPhoto("")),)
                ),
            )
        )
        with self.assertRaises(RichMessageError):
            _run(send_rich_message(bot, 777, broken))
        self.assertIsNone(bot.endpoint)

    def test_hoisting_failure_is_not_swallowed(self) -> None:
        """Ошибка хоистинга не подменяется тихим «текстовым» сообщением."""
        bot = StubBot(answer=_message())
        with mock.patch(
            "rich_message._hoist_input_files", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                _run(send_rich_message(bot, 777, InputRichMessage(markdown="привет")))
        self.assertIsNone(bot.endpoint)


if __name__ == "__main__":
    unittest.main()
