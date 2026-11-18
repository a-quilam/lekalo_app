# tests/test_stage262_template_display.py
"""
Poster 2.6.2, требование 2: человекочитаемое отображение шаблонов.

Дефект: редактор и карточка шаблона показывали канонический текст как есть,
поэтому администратор видел техническое экранирование грамматики Stage 24 —
`\\*жирный\\*`, `\\{фраза\\}`, `\\*` вместо `*`. Текст в базе при этом
канонический и не должен меняться: экранирование — служебное.

Контракт:

1. `template_markup_to_display` — точная инверсия карты экранирования
   `utils/tg_html._TEMPLATE_ESCAPES`: `\\X` → `X` только для символов
   грамматики, `[[`/`]]` → `[`/`]`, прочие обратные слэши не трогаются;
2. редактор и карточка показывают decoded-версию черновика (`*жирный*`,
   `{фраза}(ссылка)`), канонический текст в БД и в подтверждении остаётся
   прежним;
3. сохранение БЕЗ правок не создаёт новую версию: присланный текст совпал
   с decoded-версией черновика — сохраняется прежний канонический текст
   (никакого `\\*` → `\\\\*`);
4. правка текста сохраняется в каноническом виде (экранирование канонично);
5. справка объясняет: жирный/курсив/ссылки Telegram сохраняются, звёздочки
   и фигурные скобки — техническая структура, `\\*`/`\\{` — экранирование;
6. рендер поста не изменился: подстановка кирпичиков идёт по канону.

Реальные Telegram, `.env` и `post_bot.db` не используются: настоящий
`Application.process_update` поверх временной SQLite (`Harness`).

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage262_template_display -v
"""

from __future__ import annotations

import unittest

from telegram import MessageEntity

from tests import ui_texts_expect as cat
from utils.tg_html import template_markup_to_display

try:
    from template_state import load_template_session
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        Harness,
        scenario_test,
    )
    from tests.test_poster2_241_markup_acceptance import (
        _all_versions,
        _render_version,
        _version_bricks,
        _version_content,
    )
    from tests.test_stage27b_announcement_ui import _seed_personal_template

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


# Канонический текст: литеральные звёздочки экранированы, ссылка — явной
# формой `{подпись}(URL)`, кирпичик [main] привязывается как обычно.
CANONICAL = "**Итог**: \\*жирный\\* текст [main]\n{фраза}(https://example.com)"
DISPLAYED = "**Итог**: *жирный* текст [main]\n{фраза}(https://example.com)"


def _state() -> dict:
    return load_template_session(DEFAULT_USER_ID) or {}


# --- Decoder: точная инверсия карты экранирования ---------------------------


class DecoderUnitTests(unittest.TestCase):
    """`template_markup_to_display` — публичный decoder канонического текста."""

    def test_backslash_escapes_are_removed(self) -> None:
        self.assertEqual(template_markup_to_display("\\*жирный\\*"), "*жирный*")
        self.assertEqual(
            template_markup_to_display("\\{фраза\\}(ссылка)"), "{фраза}(ссылка)"
        )

    def test_every_escape_is_inverted(self) -> None:
        escapes = {
            "\\\\": "\\",
            "\\*": "*",
            "\\_": "_",
            "\\~": "~",
            "\\|": "|",
            "\\`": "`",
            "\\(": "(",
            "\\)": ")",
            "\\{": "{",
            "\\}": "}",
            "[[": "[",
            "]]": "]",
        }
        for canonical, shown in escapes.items():
            with self.subTest(canonical=canonical):
                self.assertEqual(template_markup_to_display(canonical), shown)

    def test_foreign_backslashes_are_untouched(self) -> None:
        # `\\t`, `\\d` и подобные — не экранирование грамматики: это текст.
        self.assertEqual(template_markup_to_display("C:\\tmp"), "C:\\tmp")
        self.assertEqual(template_markup_to_display("a\\b"), "a\\b")

    def test_bricks_and_markers_stay(self) -> None:
        self.assertEqual(
            template_markup_to_display("**Итог**: [main]"), "**Итог**: [main]"
        )
        # `[main]]` — кирпичик и литеральная `]`: токен не разбирается заново.
        self.assertEqual(template_markup_to_display("[main]]"), "[main]]")

    def test_plain_text_is_unchanged(self) -> None:
        text = "Обычный текст без разметки, [main] и 100.50"
        self.assertEqual(template_markup_to_display(text), text)


# --- Интеграция: редактор, карточка, сохранение -----------------------------
@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TemplateDisplayTests(unittest.TestCase):
    """Администратор видит шаблон без технических backslash-escape."""

    @scenario_test
    async def test_editor_and_card_hide_technical_escapes(self, h: Harness) -> None:
        template_id, _revision, _bricks = _seed_personal_template(h, content=CANONICAL)
        await h.text("/templates")
        await h.callback(f"tp_open:{template_id}")

        card = h.screen_text()
        self.assertIn("*жирный*", card, "карточка показывает литерал без слешей")
        self.assertIn("{фраза}(https://example.com)", card)
        self.assertNotIn("\\*жирный\\*", card, "в карточке остались слеши")
        self.assertNotIn("\\{фраза\\}", card, "в карточке остались слеши")

        await h.callback(f"tp_edit:{template_id}")
        editor = h.screen_text()
        self.assertIn("*жирный*", editor, "редактор показывает литерал без слешей")
        self.assertIn("{фраза}(https://example.com)", editor)
        self.assertNotIn("\\*жирный\\*", editor, "в редакторе остались слеши")
        self.assertNotIn("\\{фраза\\}", editor, "в редакторе остались слеши")
        # Канонический текст в состоянии не переписан.
        self.assertEqual(_state()["draft_text"], CANONICAL)

    @scenario_test
    async def test_save_without_changes_keeps_canonical_text(self, h: Harness) -> None:
        template_id, revision, _bricks = _seed_personal_template(h, content=CANONICAL)
        await h.text("/templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_edit:{template_id}")

        # Администратор присылает обратно ровно то, что видит, — без правок.
        await h.text(DISPLAYED)
        self.assertEqual(_state()["phase"], "confirm_save")
        self.assertEqual(
            _state()["draft_text"],
            CANONICAL,
            "сохранён прежний канонический черновик, повторного экранирования нет",
        )

        await h.callback("tp_save")
        versions = _all_versions(h, template_id)
        self.assertEqual(len(versions), 1, "новая версия не создана")
        self.assertEqual(versions[0][1], CANONICAL)
        self.assertNotIn("\\\\*", versions[0][1], "экранирование не задвоилось")
        with h.session_factory() as session:
            from models import Template

            row = session.get(Template, template_id)
            assert row is not None
            self.assertEqual(row.revision, revision, "ревизия не выросла")

    @scenario_test
    async def test_edited_text_is_saved_canonically(self, h: Harness) -> None:
        template_id, _revision, _bricks = _seed_personal_template(h, content=CANONICAL)
        await h.text("/templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_edit:{template_id}")

        text = "Итог: *жирный* текст [main]"
        await h.text(
            text,
            entities=[MessageEntity(type="bold", offset=0, length=len("Итог"))],
        )
        self.assertEqual(_state()["phase"], "confirm_save")
        self.assertEqual(
            _state()["draft_text"],
            "**Итог**: \\*жирный\\* текст [main]",
            "новый текст сохранён с каноническим экранированием",
        )
        await h.callback("tp_save")

        stored = _version_content(h, template_id, 2)
        self.assertEqual(stored, "**Итог**: \\*жирный\\* текст [main]")
        self.assertNotIn("\\\\*", stored, "экранирование задвоилось")
        self.assertEqual(
            _version_bricks(h, template_id, 2).get("main"), _bricks["main"]
        )

    @scenario_test
    async def test_help_explains_structure_and_escaping(self, h: Harness) -> None:
        template_id, _revision, _bricks = _seed_personal_template(h, content=CANONICAL)
        await h.text("/templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_edit:{template_id}")
        await h.callback(f"tp_help:{template_id}")

        screen = h.screen_text()
        cat.assert_shows(self, screen, "templates.help.formats")
        formats = cat.text("templates.help.formats")
        # Пример явной ссылки из требования 2.
        self.assertIn("{фраза}(ссылка)", formats)
        # Экранирование литеральных символов объяснено.
        self.assertIn("\\*", formats)
        # Звёздочки и фигурные скобки названы технической структурой.
        self.assertIn("структура", formats)

    @scenario_test
    async def test_rendering_of_post_is_unchanged(self, h: Harness) -> None:
        template_id, _revision, bricks = _seed_personal_template(
            h, content="**Итог**: \\*жирный\\* текст [main]"
        )
        rendered = _render_version(h, template_id, {bricks["main"]: "Летний концерт"})
        self.assertEqual(rendered, "<b>Итог</b>: *жирный* текст Летний концерт")
        self.assertNotIn("\\\\", rendered)
        self.assertNotIn("&lt;", rendered)

    @scenario_test
    async def test_literal_square_brackets_shown_once(self, h: Harness) -> None:
        """`[[`/`]]` — экранирование литеральных скобок, а не текст."""
        template_id, _revision, _bricks = _seed_personal_template(
            h, content="Код [[не кирпичик]] и [main]"
        )
        await h.text("/templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_edit:{template_id}")

        screen = h.screen_text()
        self.assertIn("Код [не кирпичик] и [main]", screen)
        self.assertNotIn("[[не кирпичик]]", screen)
        self.assertEqual(_state()["draft_text"], "Код [[не кирпичик]] и [main]")

        # Возврат того же текста без правок: канон не задваивается.
        await h.text("Код [не кирпичик] и [main]")
        self.assertEqual(
            _state()["draft_text"],
            "Код [[не кирпичик]] и [main]",
        )

    @scenario_test
    async def test_explicit_telegram_formatting_is_not_dropped(
        self, h: Harness
    ) -> None:
        """Новое выделение из клиента Telegram не считается «без правок»."""
        template_id, _revision, _bricks = _seed_personal_template(
            h, content="Итог: *жирный* текст [main]"
        )
        await h.text("/templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_edit:{template_id}")

        text = "Итог: *жирный* текст [main]"
        offset = text.index("текст")
        await h.text(
            text,
            entities=[MessageEntity(type="bold", offset=offset, length=5)],
        )
        self.assertEqual(_state()["phase"], "confirm_save")
        self.assertEqual(
            _state()["draft_text"],
            "Итог: \\*жирный\\* **текст** [main]",
            "выделение из Telegram сохранено, литерал экранирован",
        )
