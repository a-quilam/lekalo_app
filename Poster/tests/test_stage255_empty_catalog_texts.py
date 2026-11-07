# tests/test_stage255_empty_catalog_texts.py
"""
Poster 2.5.5 §3 — пустые блоки каталога не ломают экран.

Каталог `config/ui_texts.yaml` — канон текстов, и владелец вправе оставить
строку пустой. Три таких ключа влияют на сборку экранов:

- `wizard.question.optional` — подсказка «поле можно пропустить»;
- `wizard.question.missing` — «поле пока не заполнено»;
- `wizard.extras.intro` — вступление экрана дополнительных полей.

Пустая строка в каталоге не должна превращаться в пустую строку-разделитель
(`\\n\\n\\n`) или в двойной перевод строки: блок обязан исчезнуть целиком
вместе со своим разделителем. Непустые блоки при этом остаются на месте и
разделены ровно одной пустой строкой.

Тесты идут через настоящее приложение, FakeBot и временную SQLite
(`tests/integration_harness.py`): реальная `post_bot.db`, `.env` и сеть
Telegram не используются.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage255_empty_catalog_texts -v
"""

from __future__ import annotations

import unittest

try:
    from announcement_store import (
        FIELD_STATUS_MISSING,
        FIELD_STATUS_SKIPPED,
        FIELD_STATUS_VALID,
        AnnouncementFieldState,
    )
    from handlers import wizard_questions
    from tests import ui_texts_expect as cat
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.test_stage254_field_edit_inplace import _only_active
    from tests.test_stage255_editor_navigation import (
        _delivered_extras,
        _open_extras,
    )
    from utils.ui_texts import join_blocks, text_blocks

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


EMPTY_CATALOG_KEYS = (
    "wizard.question.optional",
    "wizard.question.missing",
    "wizard.extras.intro",
)


def _field(status: str, *, raw_value: object | None = None) -> AnnouncementFieldState:
    """Короткое текстовое поле с заданным состоянием значения."""
    return AnnouncementFieldState(
        brick_id=1,
        code="code",
        title="Название",
        value_type="short_text",
        status=status,
        raw_value=raw_value,
        rules={},
    )


# --- сборка текста ---------------------------------------------------------


class EmptyCatalogJoinTests(unittest.TestCase):
    """`join_blocks` — единственный штатный способ склеить блоки экрана."""

    def test_empty_catalog_keys_really_are_empty(self) -> None:
        for key in EMPTY_CATALOG_KEYS:
            with self.subTest(key=key):
                self.assertTrue(
                    cat.is_empty_block(key),
                    f"{key} должен быть пуст в каталоге",
                )

    def test_empty_block_does_not_leave_separator(self) -> None:
        self.assertEqual(
            text_blocks(["Заголовок", "", "Подсказка"]),
            [
                "Заголовок",
                "Подсказка",
            ],
        )
        self.assertEqual(
            join_blocks(["Заголовок", "", "Подсказка"]), "Заголовок\n\nПодсказка"
        )

    def test_only_empty_blocks_give_empty_text(self) -> None:
        self.assertEqual(join_blocks(["", "   ", None]), "")

    def test_non_empty_blocks_keep_exactly_one_separator(self) -> None:
        joined = join_blocks(["Заголовок", "Подсказка", "Сейчас: X"])
        self.assertEqual(joined, "Заголовок\n\nПодсказка\n\nСейчас: X")
        self.assertNotIn("\n\n\n", joined)


# --- вопрос без optional-блока --------------------------------------------


class QuestionWithoutOptionalBlockTests(unittest.TestCase):
    """(а) Вопрос без пустых подсказок: ни дыры, ни двойного перевода строки."""

    def test_missing_value_question_has_no_empty_separator(self) -> None:
        field = _field(FIELD_STATUS_MISSING)
        text = wizard_questions.question_text(field, 0, 1)
        cat.assert_no_empty_block(self, text, "wizard.question.optional")
        cat.assert_no_empty_block(self, text, "wizard.question.missing")
        self.assertEqual(
            text,
            "\n\n".join(
                [
                    cat.text(
                        "wizard.question.header", step=1, total=1, title="Название"
                    ),
                    cat.text("wizard.hint.short_text"),
                ]
            ),
            "пустые блоки каталога оставили лишние строки",
        )

    def test_skipped_value_keeps_its_block_only(self) -> None:
        field = _field(FIELD_STATUS_SKIPPED)
        text = wizard_questions.question_text(field, 1, 3)
        cat.assert_shows(self, text, "wizard.question.skipped")
        cat.assert_no_empty_block(self, text, "wizard.question.optional")
        cat.assert_no_empty_block(self, text, "wizard.question.missing")


# --- extras без вступления -------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ExtrasWithoutIntroTests(unittest.TestCase):
    """(б) Экран доп. полей без вступления: заголовок и список, без дыры."""

    @scenario_test
    async def test_extras_screen_has_no_double_newline(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered_extras(h)
        active = _only_active(h, draft.id)
        await _open_extras(h, active.id)

        screen = h.screen_text()
        cat.assert_no_empty_block(self, screen, "wizard.extras.intro")
        self.assertTrue(
            screen.startswith(cat.text("wizard.extras.title")),
            f"экран не начинается с заголовка: {screen[:120]!r}",
        )
        self.assertNotIn("\n\n\n", screen)
        self.assertNotIn("\n\n\n", screen.strip("\n"))


# --- непустые блоки -------------------------------------------------------


class NonEmptyBlocksIntactTests(unittest.TestCase):
    """(в) При непустых значениях все блоки на месте и не склеиваются."""

    def test_filled_value_question_keeps_all_blocks(self) -> None:
        field = _field(FIELD_STATUS_VALID, raw_value="Значение")
        text = wizard_questions.question_text(field, 2, 4)
        cat.assert_shows(self, text, "wizard.question.header", step=3, total=4)
        cat.assert_shows(self, text, "wizard.hint.short_text")
        cat.assert_shows(self, text, "wizard.question.current_value")
        self.assertNotIn("\n\n\n", text)
        self.assertEqual(
            text.split("\n\n"),
            [
                cat.text("wizard.question.header", step=3, total=4, title="Название"),
                cat.text("wizard.hint.short_text"),
                cat.text("wizard.question.current_value", value="Значение"),
            ],
            "непустые блоки склеились или потерялись",
        )

    def test_error_block_added_after_values(self) -> None:
        field = _field(FIELD_STATUS_VALID, raw_value="Значение")
        text = wizard_questions.question_text(field, 0, 2, error="Неверный формат")
        self.assertNotIn("\n\n\n", text)
        cat.assert_shows(self, text, "wizard.question.error", message="Неверный формат")
        self.assertTrue(
            text.endswith(
                "\n\n" + cat.text("wizard.question.error", message="Неверный формат")
            )
        )


if __name__ == "__main__":
    unittest.main()
