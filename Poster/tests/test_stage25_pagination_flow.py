# tests/test_stage25_pagination_flow.py
"""
Poster 2.5 §14: интеграционные проверки единой пагинации в реальном боте.

Юнит-тесты чистой логики живут в `tests/test_utils_pagination.py`; здесь —
пользовательский контракт через настоящее `Application.process_update`,
FakeBot и временную SQLite:

- список из 5 объектов: навигации, индикатора и служебных счётчиков нет;
- 6 объектов: одна строка `1/2 | ▶️` без левой стрелки, на второй странице
  `◀️ | 2/2` без правой;
- после удаления последней записи последней страницы список становится
  коротким: пагинация исчезает, состояние хранит допустимую страницу;
- нажатие индикатора (no-op callback) не меняет экран и не мешает открытому
  диалогу: ответ на callback есть, stale-alert и новых сообщений нет.

Живой Telegram, рабочая `post_bot.db` и сеть не используются.

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage25_pagination_flow -v
"""

from __future__ import annotations

import unittest

from place_store import create_place
from template_state import load_template_session
from template_store import create_personal_template
from tests.integration_harness import (
    DEFAULT_USER_ID,
    HARNESS_IMPORT_ERROR,
    Harness,
    scenario_test,
)
from user_store import get_or_create_user

try:
    INTEGRATION_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_ERROR = str(e)

CONVERSATION_NAME = "announcement_wizard"
NOOP = ("1/2", "noop:1:2")
NEXT = ("▶️", "pl_page:5")


def _callbacks(h: Harness) -> list[str]:
    return [data or "" for _label, data in h.screen_buttons()]


def _noop_buttons(h: Harness) -> list[tuple[str, str | None]]:
    return [
        (label, data)
        for label, data in h.screen_buttons()
        if data is not None and data.startswith("noop:")
    ]


def _pagination_buttons(h: Harness) -> list[tuple[str, str | None]]:
    return [
        (label, data)
        for label, data in h.screen_buttons()
        if data is not None
        and (data.startswith("noop:") or data.startswith("pl_page:"))
    ]


def _seed_places(h: Harness, count: int) -> list[int]:
    with h.session_factory() as session:
        user = get_or_create_user(session, DEFAULT_USER_ID, username="u")
        place_ids = []
        for index in range(count):
            place = create_place(
                session,
                f"Зал страниц {index + 1}",
                city="Тестовый город",
                created_by_user_id=user.id,
            )
            place_ids.append(place.id)
    return place_ids


async def _open_all_places(h: Harness) -> None:
    await h.text("/start")
    await h.callback("places")
    await h.callback("pl_all")
    assert "📚 Все места" in h.screen_text(), h.screen_text()


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class PaginationFlowTests(unittest.TestCase):
    """§14: 5/страница, `◀️ | 2/4 | ▶️`, никаких «Показано …»."""

    @scenario_test
    async def test_five_places_have_no_pagination(self, h: Harness) -> None:
        _seed_places(h, 5)
        await _open_all_places(h)

        self.assertEqual(h.screen_text().count("Зал страниц"), 5)
        self.assertNotIn("Показано", h.screen_text())
        self.assertEqual(_pagination_buttons(h), [], "навигация при 5 записях")

    @scenario_test
    async def test_six_places_paginate_one_row_with_indicator(self, h: Harness) -> None:
        _seed_places(h, 6)
        await _open_all_places(h)

        self.assertEqual(
            _pagination_buttons(h), [NOOP, NEXT], "первая страница: индикатор и ▶️"
        )
        self.assertEqual(h.screen_text().count("Зал страниц"), 5)
        self.assertNotIn("Показано", h.screen_text())

        await h.callback("pl_page:5")
        self.assertIn("Зал страниц 6", h.screen_text())
        self.assertEqual(
            _pagination_buttons(h),
            [("◀️", "pl_page:0"), ("2/2", "noop:2:2")],
            "последняя страница без правой стрелки",
        )


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class DeleteLastPageTests(unittest.TestCase):
    """Список после удаления: пустой страницы с активными стрелками нет."""

    async def _seed_templates(self, h: Harness, count: int) -> list[int]:
        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="u")
            return [
                create_personal_template(
                    session,
                    actor_telegram_user_id=DEFAULT_USER_ID,
                    name=f"Шаблон {index + 1:02d}",
                    content="Шапка\n[название]\nПодвал",
                ).template_id
                for index in range(count)
            ]

    @scenario_test
    async def test_deleting_only_item_of_last_page_removes_pagination(
        self, h: Harness
    ) -> None:
        template_ids = await self._seed_templates(h, 6)
        await h.text("/start")
        await h.text("/templates")

        self.assertIn(("1/2", "noop:1:2"), h.screen_buttons())
        await h.callback("tp_page:1")
        self.assertIn(("2/2", "noop:2:2"), h.screen_buttons())
        self.assertIn("Шаблон 06", h.screen_text())

        await h.callback(f"tp_open:{template_ids[-1]}")
        await h.callback(f"tp_delete:{template_ids[-1]}")
        await h.callback(f"tp_delete_yes:{template_ids[-1]}")

        # Осталось пять шаблонов: пагинации больше нет, показываются все.
        self.assertIn("Шаблон 05", h.screen_text())
        self.assertNotIn("Шаблон 06", h.screen_text())
        self.assertEqual(
            [
                label
                for label, data in h.screen_buttons()
                if (data or "").startswith("noop:")
            ],
            [],
            "индикатор остался после исчезновения второй страницы",
        )
        self.assertNotIn(
            "tp_page:",
            " ".join(_callbacks(h)),
            "стрелки остались при пяти шаблонах",
        )
        state = load_template_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertEqual(state.get("page"), 0, "состояние осталось на пустой странице")


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class NoopIndicatorTests(unittest.TestCase):
    """Индикатор — no-op: экран и диалоги не страдают от нажатия."""

    @scenario_test
    async def test_indicator_press_keeps_screen_unchanged(self, h: Harness) -> None:
        _seed_places(h, 6)
        await _open_all_places(h)
        text_before = h.screen_text()
        buttons_before = h.screen_buttons()
        self.assertIn(NOOP, buttons_before)
        h.bot.calls.clear()

        await h.callback("noop:1:2")

        self.assertEqual(
            h.screens(), [], "индикатор отрисовал новое сообщение вместо no-op"
        )
        self.assertEqual(h.edits(), [], "индикатор перерисовал экран")
        self.assertEqual(len(h.answers()), 1, "спиннер не закрыт ответом на callback")
        self.assertEqual(h.answer_texts(), [], "индикатор показал alert")
        self.assertTrue(text_before and buttons_before)

    @scenario_test
    async def test_indicator_does_not_break_open_dialog(self, h: Harness) -> None:
        await h.text("/start")
        await h.text("/new_wizard")
        self.assertIsNotNone(
            h.dialog_state(name=CONVERSATION_NAME), "мастер анонса не открылся"
        )
        h.bot.calls.clear()

        await h.callback("noop:1:2")

        self.assertIsNotNone(
            h.dialog_state(name=CONVERSATION_NAME),
            "нажатие индикатора завершило открытый диалог",
        )
        self.assertEqual(h.answer_texts(), [], "индикатор показал stale-alert")
        self.assertEqual(h.screens(), [], "индикатор перерисовал экран диалога")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
