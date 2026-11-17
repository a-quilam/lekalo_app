# tests/test_stage262_outside_back.py
"""
Poster 2.6.2 (requirement 1): «◀️ Назад» на подсказке неожиданного ввода.

Контракт:

1. подсказка на обычный текст вне диалога перерисовывается на экране и даёт
   ровно одну кнопку «◀️ Назад» (`nav_back`), если у экрана есть источник
   возврата; из главного меню источника нет — кнопки нет;
2. возврат идёт теми же входами разделов, что и у справки (`help_back`):
   страница списка, карточка места, редактор шаблона, шаг мастера
   восстанавливаются по персистентным сессиям/resume-снимку;
3. валидный ввод в явной фазе раздела подсказкой не перехватывается;
4. повторное нажатие и сбой Telegram не оставляют пользователя без экрана.

Реальные Telegram, `.env` и `post_bot.db` не используются: настоящий
`Application.process_update` поверх временной SQLite (`Harness`).

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage262_outside_back -v
"""

from __future__ import annotations

import unittest

from tests import ui_texts_expect as cat

try:
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        inline_buttons,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        _latest_draft,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


BACK_DATA = "nav_back"
OUTSIDE_TEXT_KEY = "menu.outside.text"


# --- помощники -------------------------------------------------------------


def _callback_startswith(h: Harness, prefix: str) -> str:
    for record in h.calls:
        for _label, data in inline_buttons(record.get("reply_markup")):
            if data and data.startswith(prefix):
                return data
    raise AssertionError(f"нет кнопки {prefix}")


def _answers(h: Harness, draft_id: int) -> list[tuple[int, str, str]]:
    """Сохранённые значения черновика: (кирпичик, состояние, JSON значения)."""
    from models import AnnouncementValue

    with h.session_factory() as session:
        rows = (
            session.query(AnnouncementValue)
            .filter(AnnouncementValue.draft_id == draft_id)
            .all()
        )
        return sorted(
            (r.brick_id, r.state, r.value_json or "") for r in rows if r.value_json
        )


def _live_keyboards_with(h: Harness, callback_data: str) -> list[int]:
    """id живых сообщений бота, у которых кнопка `callback_data` видна сейчас."""
    dead = set(h.deleted_ids())
    # Последняя отрисовка каждого сообщения: подсказка правит существующий
    # экран, поэтому важен именно свежий reply_markup, а не любая запись.
    latest: dict[int, list[tuple[str, str | None]]] = {}
    for record in h.calls:
        if record["type"] == "edit_message":
            ids: tuple = (record.get("message_id"),)
        else:
            ids = h.bot.call_message_ids.get(id(record), ())
        for message_id in ids:
            if message_id is not None:
                latest[int(message_id)] = inline_buttons(record.get("reply_markup"))
    return [
        message_id
        for message_id, buttons in latest.items()
        if message_id not in dead
        and any(data == callback_data for _label, data in buttons)
    ]


def _button_datas(h: Harness) -> list[str | None]:
    return [data for _label, data in h.screen_buttons()]


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class OutsideBackTests(unittest.TestCase):
    """П.1: «◀️ Назад» на подсказке неожиданного ввода возвращает на источник."""

    @scenario_test
    async def test_unexpected_text_from_main_menu_has_no_back(self, h: Harness) -> None:
        await h.text("/start")
        await h.text("случайный текст")

        self.assertIn(cat.text(OUTSIDE_TEXT_KEY), h.screen_text() or "")
        self.assertEqual(_button_datas(h), [], "без источника кнопки возврата нет")

    @scenario_test
    async def test_back_from_places_list_restores_page(self, h: Harness) -> None:
        await h.text("/start")
        for index in range(6):
            _seed_place(h, name=f"Место {index}")
        await h.callback("places")
        await h.callback("pl_all")
        # Вторая страница списка (PLACES_PAGE_SIZE = 5).
        await h.callback("pl_page:5")
        self.assertIn("Место 5", h.screen_text(), "ожидалась вторая страница")

        await h.text("случайный текст")
        self.assertEqual(_button_datas(h), [BACK_DATA])
        await h.callback(BACK_DATA)

        # Та же вторая страница, а не первая.
        self.assertIn("Место 5", h.screen_text())

    @scenario_test
    async def test_back_from_place_card(self, h: Harness) -> None:
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        await h.callback("places")
        await h.callback("pl_all")
        await h.callback(f"pl_open:{place_id}")
        card_text = h.screen_text()
        self.assertTrue(card_text)

        await h.text("случайный текст")
        self.assertEqual(_button_datas(h), [BACK_DATA])
        await h.callback(BACK_DATA)

        self.assertEqual(h.screen_text(), card_text, "карточка восстановлена дословно")

    @scenario_test
    async def test_back_from_template_editor_keeps_content(self, h: Harness) -> None:
        """Редактор шаблона: подсказка на фото возвращает тот же контент."""
        await h.text("/start")
        template_id, _revision, _bricks = _seed_personal_template(
            h, name="Личный", content="Текст личного шаблона"
        )
        await h.callback("manage")
        await h.callback("manage_templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_edit:{template_id}")
        editor_text = h.screen_text()
        self.assertTrue(editor_text)

        # Текст в редакторе — валидный ввод правки (сценарий его ждёт).
        # Вне диалога редактора экран ждёт только фото.
        await h.photo("photo-in-template-editor")
        self.assertEqual(_button_datas(h), [BACK_DATA])
        await h.callback(BACK_DATA)

        # Черновик правки сохранён: тот же экран с тем же контентом.
        self.assertEqual(h.screen_text(), editor_text)

    @scenario_test
    async def test_back_to_wizard_step_preserves_answers(self, h: Harness) -> None:
        """После рестарта в группе подсказка возвращает на текущий шаг."""
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        await _start_to_template(h, place_id)
        await h.callback(_callback_startswith(h, "aw_template"))
        draft = _latest_draft(h)
        self.assertIsNotNone(draft)
        await h.text("Первый ответ")
        answers_before = _answers(h, draft.id)
        self.assertTrue(answers_before, "ответ мастера не сохранён")

        # Рестарт службы: состояние диалога потеряно. В группе авто-resume
        # не включается (анонс в группе не продолжается) — пользователь
        # видит подсказку с возвратом на текущий шаг черновика.
        await h.restart()
        await h.text("случайный текст", chat_type="group")
        self.assertEqual(_button_datas(h), [BACK_DATA], "нет возврата на шаг мастера")
        await h.callback(BACK_DATA, chat_type="group")

        self.assertRegex(
            h.screen_text(), cat.question_re(2), "не вернулись на текущий шаг"
        )
        self.assertEqual(_answers(h, draft.id), answers_before, "ответы сохранены")

    @scenario_test
    async def test_valid_input_not_intercepted(self, h: Harness) -> None:
        await h.text("/start")
        _seed_place(h)
        await h.callback("places")
        await h.callback("pl_search")
        await h.text("Дом")

        self.assertNotIn(
            cat.fragment(OUTSIDE_TEXT_KEY),
            h.screen_text() or "",
            "валидный поисковый запрос перехвачен подсказкой",
        )

    @scenario_test
    async def test_two_unexpected_messages_single_screen_one_button(
        self, h: Harness
    ) -> None:
        await h.text("/start")
        _seed_place(h)
        await h.callback("places")
        await h.text("первое неожиданное")
        await h.text("второе неожиданное")

        texts = h.screen_texts()
        hints = [text for text in texts if cat.text(OUTSIDE_TEXT_KEY) in text]
        self.assertEqual(len(hints), 1, "подсказка добавлена, а не перерисована")
        self.assertEqual(texts[-1], cat.text(OUTSIDE_TEXT_KEY))
        self.assertEqual(_button_datas(h), [BACK_DATA])
        self.assertEqual(
            len(_live_keyboards_with(h, BACK_DATA)), 1, "конкурирующие клавиатуры"
        )
        await h.callback(BACK_DATA)
        self.assertNotIn(cat.text(OUTSIDE_TEXT_KEY), h.screen_text())

    @scenario_test
    async def test_repeated_back_is_harmless(self, h: Harness) -> None:
        await h.text("/start")
        _seed_place(h)
        await h.callback("places")
        await h.text("случайный текст")
        await h.callback(BACK_DATA)
        first = h.screen_text()

        await h.callback(BACK_DATA)

        self.assertEqual(h.screen_text(), first)

    @scenario_test
    async def test_deleted_place_falls_back_safely(self, h: Harness) -> None:
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        await h.callback("places")
        await h.callback("pl_all")
        await h.callback(f"pl_open:{place_id}")
        await h.text("случайный текст")
        from models import Place

        with h.session_factory() as session:
            place = session.get(Place, place_id)
            session.delete(place)
            session.commit()

        await h.callback(BACK_DATA)

        self.assertNotIn(cat.text(OUTSIDE_TEXT_KEY), h.screen_text())

    @scenario_test
    async def test_telegram_failure_leaves_screen(self, h: Harness) -> None:
        await h.text("/start")
        _seed_place(h)
        await h.callback("places")
        await h.text("случайный текст")

        h.bot.fail_types.add("edit_message")
        try:
            await h.callback(BACK_DATA)
        finally:
            h.bot.fail_types.discard("edit_message")

        self.assertTrue(h.screen_text(), "сбой Telegram оставил пустой экран")
