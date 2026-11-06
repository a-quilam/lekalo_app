# tests/test_stage251_help_return.py
"""
Poster 2.5.1 (Dev-3): «◀️ Назад» на Rich Message справки возвращает на экран-источник.

Контракт корректирующего релиза:

1. кнопка возврата одна — «◀️ Назад» с `callback_data=help_back`; старая
   «🏠 В меню» (`main_menu`) с Rich Message убрана;
2. источник НЕ стирается: временный Rich Message удаляется, а контекст
   (персистентная сессия раздела, resume-снимок конструктора) остаётся,
   поэтому возврат перерисовывает ТОТ ЖЕ экран — страницу, поиск, фильтры,
   текущий шаг черновика, — а не главное меню;
3. если контекста для возврата нет (справка из меню/списка, `help_return`
   потерян после рестарта), безопасный фолбэк — главное меню, а не ошибка;
4. ответ на callback отправляется ровно один раз: входы разделов отвечают
   сами (`open_places` и остальные), для конструктора и меню отвечает
   `help_back` — повторный `answer` Telegram считает ошибкой запроса.

Реальные Telegram, `.env` и `post_bot.db` не используются: настоящий
`Application.process_update` поверх временной SQLite (`Harness`).

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage251_help_return -v
"""

from __future__ import annotations

import json
import unittest

from sqlalchemy import select

try:
    from handlers.announcement_wizard import RESUME_PAYLOAD_KEY
    from models import AnnouncementValue
    from place_state import load_place_session
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        inline_buttons,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        _choose_personal,
        _latest_draft,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )
    from utils import screen as screen_utils

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


BACK_LABEL = "◀️ Назад"
BACK_DATA = "help_back"
# Poster 2.5.4 §7: «ℹ️ О проекте» — вторая кнопка в той же строке справки.
ABOUT_LABEL = "ℹ️ О проекте"
ABOUT_DATA = "help_about"
LEGACY_BACK_DATA = "main_menu"
RICH_KEY = "help_rich_message_id"
MENU_TEXT = "Что хотите сделать?"


# --- помощники -------------------------------------------------------------


def _rich_records(h: Harness) -> list[dict]:
    return list(h.bot.rich_records)


def _live_keyboards(h: Harness) -> list[int]:
    """id живых сообщений бота с inline-клавиатурой (конкурирующие меню)."""
    dead = set(h.deleted_ids())
    return [
        int(message_id)
        for (_chat, message_id), entry in sorted(h.bot._message_content.items())
        if int(message_id) not in dead and entry[2] is not None
    ]


def _answers(h: Harness) -> int:
    """Сколько ответов на callback отправлено за сцену."""
    return len(h.answers())


def _stored_value(h: Harness, draft_id: int, brick_id: int) -> dict | None:
    """Сохранённое значение кирпичика (None — значение ещё не принято)."""
    with h.session_factory() as session:
        row = session.scalar(
            select(AnnouncementValue).where(
                AnnouncementValue.draft_id == draft_id,
                AnnouncementValue.brick_id == brick_id,
            )
        )
    if row is None or not row.value_json:
        return None
    return json.loads(row.value_json)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class HelpKeyboardTests(unittest.TestCase):
    """П.1 + 2.5.4 §7: на справке «◀️ Назад» и «ℹ️ О проекте» в одной строке."""

    @scenario_test
    async def test_help_has_back_and_about_buttons(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        record = _rich_records(h)[0]

        buttons = inline_buttons(record["reply_markup"])
        self.assertEqual(buttons, [(BACK_LABEL, BACK_DATA), (ABOUT_LABEL, ABOUT_DATA)])
        self.assertNotIn(
            LEGACY_BACK_DATA,
            [data for _label, data in buttons],
            "старая кнопка «🏠 В меню» осталась на Rich Message",
        )
        self.assertEqual(len(buttons), 2, "на справке две кнопки")
        self.assertEqual(
            len(record["reply_markup"].inline_keyboard),
            1,
            "кнопки справки должны стоять в одной строке",
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class HelpBackToMenuTests(unittest.TestCase):
    """П.3: справка из меню возвращает в меню (источник — сам экран меню)."""

    @scenario_test
    async def test_back_from_menu_restores_menu(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        rich_id = _rich_records(h)[0]["message_id"]
        answers_before = _answers(h)

        await h.callback(BACK_DATA)

        self.assertIn(rich_id, h.deleted_ids(), "временная справка не удалена")
        self.assertNotIn(RICH_KEY, h.user_data(), "id справки остался в состоянии")
        self.assertIn(MENU_TEXT, h.screen_text())
        self.assertEqual(
            [data for _label, data in h.screen_buttons()],
            ["pick_place", "my_announcements", "manage", "help"],
        )
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")
        self.assertEqual(_answers(h) - answers_before, 1, "ответ на callback не один")

    @scenario_test
    async def test_back_after_double_press_does_not_duplicate_screen(
        self, h: Harness
    ) -> None:
        """Повторное нажатие уже удалённой кнопки не плодит экраны."""
        await h.text("/start")
        await h.callback("help")
        await h.callback(BACK_DATA)
        await h.callback(BACK_DATA)

        self.assertIn(MENU_TEXT, h.screen_text())
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")
        self.assertEqual(len(_rich_records(h)), 1, "лишний Rich Message")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class HelpBackToSectionTests(unittest.TestCase):
    """П.2: раздел возвращается с сохранённой страницей, поиском и фильтрами."""

    @scenario_test
    async def test_back_restores_places_search_results(self, h: Harness) -> None:
        await h.text("/start")
        for number in range(3):
            _seed_place(h, name=f"Площадка {number}")
        await h.callback("places")
        await h.callback("pl_search")
        await h.text("Площадка 1")
        results_text = h.screen_text()
        self.assertIn("Результаты поиска", results_text)
        state_before = dict(load_place_session(h.user_id) or {})

        await h.callback("help")
        answers_before = _answers(h)
        await h.callback(BACK_DATA)

        self.assertIn("Результаты поиска", h.screen_text(), "не вернулись в поиск")
        self.assertNotIn(MENU_TEXT, h.screen_text(), "вернулись в меню, а не в раздел")
        # Сессия раздела не тронута справкой: запрос и фаза те же.
        self.assertEqual(load_place_session(h.user_id), state_before)
        self.assertEqual(_answers(h) - answers_before, 1, "ответ на callback не один")

    @scenario_test
    async def test_back_restores_places_list_page(self, h: Harness) -> None:
        """Страница списка «📚 Все места» возвращается вместе с номером."""
        await h.text("/start")
        for number in range(7):
            _seed_place(h, name=f"Место {number}")
        await h.callback("places")
        await h.callback("pl_all")
        # вторая страница списка
        next_page = [
            data
            for _label, data in h.screen_buttons()
            if data and data.startswith("pl_page:")
        ]
        self.assertTrue(next_page, "нет навигации по страницам списка")
        await h.callback(next_page[0])
        page_text = h.screen_text()
        state_before = dict(load_place_session(h.user_id) or {})

        await h.callback("help")
        await h.callback(BACK_DATA)

        self.assertEqual(h.screen_text(), page_text, "страница списка не восстановлена")
        self.assertEqual(load_place_session(h.user_id), state_before)

    @scenario_test
    async def test_back_restores_manage_places_dictionary(self, h: Harness) -> None:
        """«Управление → Площадки» — отдельный маршрут (`open_places_manage`)."""
        await h.text("/start")
        _seed_place(h, name="Дом культуры")
        await h.callback("manage")
        await h.callback("manage_places")
        dictionary_text = h.screen_text()
        self.assertIn("Площадки", dictionary_text)

        await h.callback("help")
        answers_before = _answers(h)
        await h.callback(BACK_DATA)

        self.assertIn("Площадки", h.screen_text(), "не вернулись в справочник")
        self.assertNotIn(MENU_TEXT, h.screen_text())
        # Справочник, а не выбор площадки для анонса.
        self.assertTrue(
            [
                data
                for _label, data in h.screen_buttons()
                if (data or "").startswith("plm_")
            ],
            "открылся не справочник площадок",
        )
        self.assertEqual(_answers(h) - answers_before, 1, "ответ на callback не один")

    @scenario_test
    async def test_back_restores_templates_section(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("manage")
        await h.callback("manage_templates")
        section_text = h.screen_text()

        await h.callback("help")
        await h.callback(BACK_DATA)

        self.assertEqual(
            h.screen_text(), section_text, "раздел шаблонов не восстановлен"
        )
        self.assertNotIn(MENU_TEXT, h.screen_text())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class HelpBackToConstructorTests(unittest.TestCase):
    """П.2: активный конструктор возвращается на ТЕКУЩИЙ шаг, снимок жив."""

    async def _open_question(self, h: Harness):
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        self.assertIn("Вопрос 1 из 1", h.screen_text())
        return bricks

    @scenario_test
    async def test_back_returns_to_current_question(self, h: Harness) -> None:
        await self._open_question(h)
        answers_before = _answers(h)

        await h.callback("help")
        payload = screen_utils.load_payload(h.user_id) or {}
        self.assertIn(RESUME_PAYLOAD_KEY, payload, "справка сняла resume-снимок")

        await h.callback(BACK_DATA)

        self.assertIn("Вопрос 1 из 1", h.screen_text(), "не вернулись на текущий шаг")
        self.assertNotIn(MENU_TEXT, h.screen_text())
        payload = screen_utils.load_payload(h.user_id) or {}
        self.assertIn(RESUME_PAYLOAD_KEY, payload, "возврат снял resume-снимок")
        # Один callback — один answer: вход конструктора сам не отвечает.
        self.assertEqual(_answers(h) - answers_before, 2, "ответы на 2 callback")
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")

    @scenario_test
    async def test_back_returns_to_photo_step(self, h: Harness) -> None:
        bricks = await self._open_question(h)
        await h.text("Текст анонса")
        self.assertIn("Фотографии анонса", h.screen_text())

        await h.callback("help")
        await h.callback(BACK_DATA)

        self.assertIn("Фотографии анонса", h.screen_text(), "шаг фото потерян")
        payload = screen_utils.load_payload(h.user_id) or {}
        self.assertIn(
            RESUME_PAYLOAD_KEY, payload, "возврат на шаг фото снял resume-снимок"
        )
        # Шаг живой: фото принимается, а не уходит в подсказку меню.
        await h.photo("photo-after-help")
        self.assertIn("photo-after-help", _latest_draft(h).photos)
        self.assertNotIn("Фото можно добавить при создании анонса", h.screen_text())
        self.assertEqual(
            (_stored_value(h, _latest_draft(h).id, bricks["main"]) or {}).get("text"),
            "Текст анонса",
        )

    @scenario_test
    async def test_help_from_constructor_keeps_resume_after_restart(
        self, h: Harness
    ) -> None:
        """П.2/П.5: справка не выход — после рестарта текст продолжает шаг."""
        bricks = await self._open_question(h)
        draft = _latest_draft(h)

        await h.restart()
        await h.text("/help")

        payload = screen_utils.load_payload(h.user_id) or {}
        self.assertIn(
            RESUME_PAYLOAD_KEY, payload, "справка после рестарта сняла resume-снимок"
        )

        await h.text("Текст после справки")
        self.assertNotIn(MENU_TEXT, h.screen_text(), "текст ушёл в меню")
        # Значение принято шагом, конструктор пошёл дальше (шаг фото).
        self.assertIn("Фотографии анонса", h.screen_text())
        self.assertEqual(
            (_stored_value(h, draft.id, bricks["main"]) or {}).get("text"),
            "Текст после справки",
            "ввод после справки не продолжил шаг",
        )
        self.assertEqual(_latest_draft(h).id, draft.id)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class HelpBackCleanupTests(unittest.TestCase):
    """П.2: Rich Message удаляется и из user_data, и из БД (§53)."""

    @scenario_test
    async def test_back_forgets_rich_message_in_memory_and_db(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        rich_id = _rich_records(h)[0]["message_id"]
        self.assertEqual(h.user_data()[RICH_KEY], rich_id)
        self.assertEqual(screen_utils.load_rich_temp(h.user_id), rich_id)

        await h.callback(BACK_DATA)

        self.assertIn(rich_id, h.deleted_ids(), "временная справка не удалена")
        self.assertNotIn(RICH_KEY, h.user_data(), "id справки остался в user_data")
        self.assertIsNone(
            screen_utils.load_rich_temp(h.user_id), "id справки остался в БД"
        )

    @scenario_test
    async def test_back_from_constructor_also_deletes_rich_message(
        self, h: Harness
    ) -> None:
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)

        await h.callback("help")
        rich_id = _rich_records(h)[0]["message_id"]
        await h.callback(BACK_DATA)

        self.assertIn(rich_id, h.deleted_ids())
        self.assertIsNone(screen_utils.load_rich_temp(h.user_id))
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class HelpBackAfterRestartTests(unittest.TestCase):
    """П.3: `help_return` живёт только в памяти — после рестарта фолбэк в меню."""

    @scenario_test
    async def test_back_after_restart_falls_back_to_menu(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("manage")
        await h.callback("help")
        rich_id = _rich_records(h)[0]["message_id"]

        await h.restart()  # user_data потерян, справка и ref экрана — в БД

        await h.callback(BACK_DATA)

        self.assertIn(rich_id, h.deleted_ids(), "справка не удалена после рестарта")
        self.assertIn(MENU_TEXT, h.screen_text(), "нет безопасного фолбэка в меню")
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")

    @scenario_test
    async def test_restart_does_not_lose_constructor_snapshot(self, h: Harness) -> None:
        """Снимок конструктора переживает рестарт и справку: данные не теряются."""
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        draft = _latest_draft(h)

        await h.restart()
        await h.callback("help")
        await h.callback(BACK_DATA)
        await h.text("Текст после рестарта и справки")

        self.assertEqual(
            (_stored_value(h, draft.id, bricks["main"]) or {}).get("text"),
            "Текст после рестарта и справки",
            "черновик потерян на возврате после рестарта",
        )
        self.assertEqual(_latest_draft(h).id, draft.id)


if __name__ == "__main__":
    unittest.main()
