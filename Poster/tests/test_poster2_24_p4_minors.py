# tests/test_poster2_24_p4_minors.py
"""
Poster 2.4, этап P4 — миноры ревизии P2 (разработчик).

Проверяется ровно то, что было найдено ревизией и закрыто этапом:

1. `docs/ui_texts.md` описывает действующий набор ключей экрана доп. полей:
   `buttons.extras.add` удалён вместе с кнопкой в P2, а `_extras_entry`
   (список показывается сразу) — нет.
2. `handlers/announcement_wizard.SCOPE_LABELS` больше не существует как
   мёртвая константа: подписи областей собираются по месту.
3. Экран «🧱 Дополнительные поля» показывает честный нейтральный текст при
   любой доменной ошибке открытия списка, а не «нет расширенного текста»
   (этот текст остаётся только для штатного случая «шаблон без extended»).

Тесты идут через настоящее `Application.process_update`, реальные callback-
маршруты и FakeBot (`tests.integration_harness`); подмена — только на границе
доменного вызова, чтобы получить нужную ветку ошибки без порчи данных.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        scenario_test,
    )
    from tests.test_poster2_24_p2_wizard_tester import (
        EXTRAS_TITLE,
        _callbacks,
        _deliver,
        _latest_draft,
        _specs,
    )
    from tests.test_stage27b_announcement_ui import _open_edit

    IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    IMPORT_ERROR = str(exc)


ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "ui_texts.md"
WIZARD_SOURCE = ROOT / "handlers" / "announcement_wizard.py"

NO_EXTENDED_FRAGMENT = "нет расширенной версии текста"
UNAVAILABLE_FRAGMENT = "Не удалось показать дополнительные поля"


class UiTextsDocsTests(unittest.TestCase):
    """Минор 1: документ описывает фактический набор ключей."""

    def setUp(self) -> None:
        self.docs = DOCS.read_text(encoding="utf-8")

    def test_docs_do_not_list_removed_add_button_as_active(self) -> None:
        active_rows = [
            line
            for line in self.docs.splitlines()
            if line.startswith("|") and "`buttons.extras.add`" in line
        ]
        self.assertEqual(
            active_rows,
            [],
            "кнопки «➕ Добавить поле» больше нет — ключ не должен "
            "числиться в таблице действующих ключей",
        )

    def test_docs_list_actual_extras_keys(self) -> None:
        for key in (
            "wizard.extras.title",
            "wizard.extras.intro",
            "wizard.extras.empty",
            "wizard.extras.no_extended",
            "wizard.extras.unavailable",
            "buttons.extras.edit",
            "buttons.extras.delete",
        ):
            self.assertIn(key, self.docs, key)


class DeadScopeLabelsTests(unittest.TestCase):
    """Минор 2: `SCOPE_LABELS` действительно не использовался и удалён."""

    def test_scope_labels_is_gone(self) -> None:
        source = WIZARD_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assigned = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.assertNotIn("SCOPE_LABELS", assigned)
        self.assertNotIn("SCOPE_LABELS", source)


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class ExtrasErrorNoticeTests(unittest.TestCase):
    """Минор 3: ошибка открытия списка — нейтральный доменный текст."""

    async def _deliver_extended(self, h: Harness) -> None:
        await h.text("/start")
        await _deliver(
            h,
            specs=_specs(
                ("main", "Основа", "short_text", {}),
                ("e1", "Доп-А", "short_text", {}),
            ),
            content="A [main] B",
            extended="A [main] B [e1]",
        )

    @scenario_test
    async def test_domain_error_shows_neutral_text_not_no_extended(self, h):
        """
        Любая доменная ошибка списка (например, кирпичик стал недоступен)
        не выдаёт себя за «у шаблона нет расширенного текста».
        """
        from announcement_wizard import AnnouncementBrickError
        from handlers import announcement_wizard as module

        await self._deliver_extended(h)
        await _open_edit(h)
        data = _callbacks(h, "aw_extras")
        self.assertTrue(data, f"нет кнопки доп. полей: {h.screen_buttons()}")

        error = AnnouncementBrickError(
            "Кирпичик #1 недоступен для этого пользователя/места."
        )
        with patch.object(module, "_extras_entries", side_effect=error):
            await h.callback(data[0])

        text = h.screen_text()
        self.assertIn(UNAVAILABLE_FRAGMENT, text, text[:400])
        self.assertNotIn(
            NO_EXTENDED_FRAGMENT,
            text,
            "ошибка недоступного кирпичика выдана за отсутствие " "расширенного текста",
        )
        # Экран остаётся рабочим: кнопки на месте, «внутренней ошибки» нет.
        self.assertTrue(
            [label for label, _data in h.screen_buttons()],
            "экран доп. полей не должен оставаться без кнопок",
        )
        self.assertNotIn("Что-то пошло не так", text)

    @scenario_test
    async def test_template_without_extended_keeps_its_own_text(self, h):
        """Штатный случай «нет расширенного текста» не изменился."""
        await h.text("/start")
        await _deliver(
            h,
            specs=_specs(("main", "Основа", "short_text", {})),
            content="A [main] B",
            extended=None,
        )
        await _open_edit(h)
        # Кнопки «доп. поля» у такого шаблона нет — открываем экран её
        # штатным callback'ом, как это делает устаревшая кнопка.
        draft = _latest_draft(h)
        ui = h.user_data().get("announcement_ui") or {}
        revision = ui.get("revision") or draft.revision
        await h.callback(f"aw_extras:{draft.id}:{revision}")

        text = h.screen_text()
        self.assertIn(EXTRAS_TITLE, text, text[:400])
        self.assertIn(NO_EXTENDED_FRAGMENT, text, text[:400])
        self.assertNotIn(UNAVAILABLE_FRAGMENT, text)


if __name__ == "__main__":
    unittest.main()
