# tests/test_stage252_template_editor.py
"""
Poster 2.5.2, T4 (дефекты 5–7 §5, сценарии 12–14): редактор шаблонов.

Дефект 5: первое корректное сообщение в расширенном редакторе не принималось.
Корневая причина — конвертер entities (`utils/tg_html.py`) резал текст по
границе, не кратной кодовой точке UTF-16: `bold` на эмодзи давал
`UnicodeDecodeError`, сообщение падало в error handler, фаза оставалась
`section_edit`. Один механизм у простого и расширенного редактора, поэтому
проверяются оба.

Пользовательский результат (настоящее приложение, FakeBot, временная SQLite):

- сообщение с форматированием на эмодзи принимается сразу и показывает
  подтверждение сохранения;
- форматирование (`**…**`, `*…*`), скрытая ссылка `подпись(URL)` и коды
  кирпичиков `[код]` доходят до версии шаблона без потерь;
- литеральные `{{…}}` сохраняются как текст;
- простая и расширенная версии правятся независимо.
"""

from __future__ import annotations

import unittest

from telegram import MessageEntity

from utils.tg_html import entities_to_html, entities_to_template_markup


def ent(kind: str, offset: int, length: int, **kwargs) -> MessageEntity:
    """MessageEntity с явным типом (минимальный конструктор для тестов)."""
    return MessageEntity(type=kind, offset=offset, length=length, **kwargs)


def span(text: str, label: str, start: int = 0) -> tuple[int, int]:
    """Смещение и длина подстроки в код-юнитах UTF-16 (единицы Telegram)."""
    index = text.index(label, start)
    offset = len(text[:index].encode("utf-16-le")) // 2
    return offset, len(label.encode("utf-16-le")) // 2


class EmojiBoundaryUnitTests(unittest.TestCase):
    """Граница entity внутри суррогатной пары не роняет захват текста."""

    def test_bold_half_emoji_becomes_whole_emoji(self) -> None:
        text = "🔥 [название]"
        self.assertEqual(
            entities_to_template_markup(text, [ent("bold", 0, 1)]),
            "**🔥** [название]",
        )

    def test_bold_half_emoji_in_html(self) -> None:
        text = "🔥 [название]"
        self.assertEqual(
            entities_to_html(text, [ent("bold", 0, 1)]), "<b>🔥</b> [название]"
        )

    def test_offset_inside_surrogate_pair_is_aligned(self) -> None:
        text = "🔥🔥 итог"
        # offset=1 — середина первой пары, length=1 — половина второй.
        self.assertEqual(
            entities_to_template_markup(text, [ent("bold", 1, 1)]),
            "**🔥**🔥 итог",
        )

    def test_emoji_inside_code_does_not_crash(self) -> None:
        text = "🔥 итог"
        self.assertEqual(
            entities_to_template_markup(text, [ent("code", 0, 1)]),
            "`🔥` итог",
        )
        self.assertEqual(
            entities_to_html(text, [ent("code", 0, 1)]), "<code>🔥</code> итог"
        )

    def test_zero_length_entity_keeps_text(self) -> None:
        text = "🔥 итог"
        self.assertEqual(entities_to_template_markup(text, [ent("bold", 0, 0)]), text)
        self.assertEqual(entities_to_html(text, [ent("bold", 0, 0)]), text)

    def test_aligned_emoji_entity_is_unchanged(self) -> None:
        text = "🔥 итог"
        self.assertEqual(
            entities_to_template_markup(text, [ent("bold", 0, 2)]),
            "**🔥** итог",
        )

    def test_entity_beyond_text_is_dropped_with_log(self) -> None:
        text = "🔥 итог"
        for convert in (entities_to_template_markup, entities_to_html):
            with self.subTest(convert=convert.__name__):
                with self.assertLogs("utils.tg_html", level="WARNING"):
                    result = convert(text, [ent("bold", 0, 99)])
                self.assertIn("🔥", result)
                self.assertIn("итог", result)

    def test_emoji_link_keeps_url(self) -> None:
        text = "🔥 сайт"
        self.assertEqual(
            entities_to_template_markup(
                text, [ent("text_link", 0, 1, url="https://e.example/x")]
            ),
            "🔥(https://e.example/x) сайт",
        )


# --- Интеграция: настоящее приложение ---------------------------------------
try:
    from template_state import load_template_session
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.test_poster2_241_markup_acceptance import (
        _version_content,
        _version_extended,
    )
    from tests.test_stage27b_announcement_ui import _seed_personal_template

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


def _state() -> dict:
    return load_template_session(DEFAULT_USER_ID) or {}


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ExtendedEditorFirstMessageTests(unittest.TestCase):
    """Дефект 5: первое сообщение расширенного редактора принимается сразу."""

    @scenario_test
    async def test_first_message_with_emoji_bold_is_accepted(self, h) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            specs=[("main", "Основное поле", "short_text", {})],
            content="[main]",
            extended_content="[main]",
        )
        await h.text("/templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_edit_ext:{template_id}")
        self.assertEqual(_state()["phase"], "section_edit")

        text = "🔥 Итог: [main]"
        await h.text(text, entities=[ent("bold", 0, 1)])

        # Сообщение принято сразу: подтверждение, а не повторный экран ввода.
        self.assertEqual(_state()["phase"], "confirm_save")
        self.assertEqual(_state()["draft_text"], "**🔥** Итог: [main]")
        self.assertIn("**🔥** Итог: [main]", h.screen_text())
        self.assertIn("tp_save", [data for _label, data in h.screen_buttons()])
        self.assertNotIn("Что-то пошло не так", h.screen_text())

        await h.callback("tp_save")
        self.assertEqual(_version_content(h, template_id, 2), "[main]")
        self.assertEqual(_version_extended(h, template_id, 2), "**🔥** Итог: [main]")

    @scenario_test
    async def test_simple_editor_shares_the_same_mechanism(self, h) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            content="[main]",
            extended_content="[main]",
        )
        await h.text("/templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_edit:{template_id}")

        text = "🔥 Основной [main]"
        await h.text(text, entities=[ent("bold", 0, 1)])

        self.assertEqual(_state()["phase"], "confirm_save")
        self.assertEqual(_state()["draft_text"], "**🔥** Основной [main]")
        await h.callback("tp_save")
        self.assertEqual(_version_content(h, template_id, 2), "**🔥** Основной [main]")
        self.assertEqual(_version_extended(h, template_id, 2), "[main]")

    @scenario_test
    async def test_formatting_link_and_bricks_survive_round_trip(self, h) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            specs=[
                ("main", "Основное поле", "short_text", {}),
                ("extra", "Доп. поле", "short_text", {}),
            ],
            content="[main]",
            extended_content="[main]\n[extra]",
        )
        await h.text("/templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_edit_ext:{template_id}")

        text = "🎉 Итог [main] [extra] Сайт {{текст}}"
        entities = [
            ent("bold", *span(text, "🎉")),
            ent("italic", *span(text, "Итог")),
            ent(
                "text_link",
                *span(text, "Сайт"),
                url="https://e.example/x",
            ),
        ]
        await h.text(text, entities=entities)

        self.assertEqual(_state()["phase"], "confirm_save")
        draft = _state()["draft_text"]
        self.assertIn("**🎉**", draft)
        self.assertIn("*Итог*", draft)
        self.assertIn("[main]", draft)
        self.assertIn("[extra]", draft)
        self.assertIn(r"\{\{текст\}\}", draft)
        self.assertIn("(https://e.example/x)", draft)

        await h.callback("tp_save")
        self.assertEqual(_version_extended(h, template_id, 2), draft)
        # Простая версия не тронута расширенной правкой.
        self.assertEqual(_version_content(h, template_id, 2), "[main]")
