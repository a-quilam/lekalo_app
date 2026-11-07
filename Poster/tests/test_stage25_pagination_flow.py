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
  диалогу: ответ на callback есть, stale-alert и новых сообщений нет;
- маршруты конструктора анонса (список мест, личные шаблоны, доп. поля)
  подчиняются тому же контракту, хотя их callback хранит номер страницы.

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
from tests import ui_texts_expect as cat
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


def _wizard_nav(h: Harness, prefix: str) -> list[tuple[str, str | None]]:
    """Строка навигации конкретного маршрута конструктора (со стрелками)."""
    return [
        (label, data)
        for label, data in h.screen_buttons()
        if data is not None and (data.startswith("noop:") or data.startswith(prefix))
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
class WizardPaginationTests(unittest.TestCase):
    """
    Конструктор анонса — те же 5/страница и та же строка `◀️ | 2/4 | ▶️`.

    Маршруты конструктора хранят в callback НОМЕР страницы, поэтому
    проверяются отдельно от списков с префиксом `pl_page:<offset>`.
    """

    @scenario_test
    async def test_wizard_place_list_paginates_five_per_page(self, h: Harness) -> None:
        _seed_places(h, 6)
        await h.text("/start")
        await h.text("/new_wizard")
        await h.callback("aw_start")
        await h.callback("aw_new")
        await h.callback("aw_place_all:0")

        self.assertEqual(
            len(
                cat.pattern(
                    "wizard.render_all_places.vsego_stranica", total=6, value=1
                ).findall(h.screen_text())
            ),
            1,
        )
        self.assertEqual(
            len([data for data in _callbacks(h) if data.startswith("aw_place_open:")]),
            5,
            "список мест конструктора — пять записей на страницу",
        )
        self.assertEqual(
            [
                (label, data)
                for label, data in h.screen_buttons()
                if (data or "").startswith(("noop:", "aw_place_all:"))
            ],
            [("1/2", "noop:1:2"), ("▶️", "aw_place_all:1")],
            "первая страница списка мест конструктора",
        )

        await h.callback("aw_place_all:1")
        cat.assert_shows(
            self,
            h.screen_text(),
            "wizard.render_all_places.vsego_stranica",
            total=6,
            value=2,
        )
        self.assertEqual(
            [
                (label, data)
                for label, data in h.screen_buttons()
                if (data or "").startswith(("noop:", "aw_place_all:"))
            ],
            [("◀️", "aw_place_all:0"), ("2/2", "noop:2:2")],
            "последняя страница без правой стрелки",
        )

    @scenario_test
    async def test_wizard_place_list_has_no_nav_for_five(self, h: Harness) -> None:
        _seed_places(h, 5)
        await h.text("/start")
        await h.text("/new_wizard")
        await h.callback("aw_start")
        await h.callback("aw_new")
        await h.callback("aw_place_all:0")

        self.assertEqual(
            len([data for data in _callbacks(h) if data.startswith("aw_place_open:")]),
            5,
            "пять записей помещаются на одну страницу",
        )
        self.assertEqual(
            [
                (label, data)
                for label, data in h.screen_buttons()
                if (data or "").startswith(("noop:", "aw_place_all:"))
                and data != "aw_place_all:0"
            ],
            [],
            "на пяти записях пагинации нет",
        )

    @scenario_test
    async def test_wizard_template_screen_paginates_five_per_page(
        self, h: Harness
    ) -> None:
        """Личные шаблоны в шаге 2 конструктора: 5 на страницу и индикатор."""
        from tests.test_stage27b_announcement_ui import _confirm_place

        place_ids = _seed_places(h, 1)
        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="u")
            for index in range(7):
                create_personal_template(
                    session,
                    actor_telegram_user_id=DEFAULT_USER_ID,
                    name=f"Личный шаблон {index + 1}",
                    content="Шапка\n[название]\nПодвал",
                )
        await h.text("/start")
        await h.text("/new_wizard")
        await h.callback("aw_start")
        await h.callback("aw_new")
        await h.callback(f"aw_place_open:{place_ids[0]}")
        await _confirm_place(h)

        cat.assert_shows(
            self, h.screen_text(), "wizard.render_template.shag_2_vyberite_shablon"
        )
        # Стандартный шаблон места — отдельная кнопка того же префикса;
        # страницу делят только личные шаблоны.
        personal = [
            (label, data)
            for label, data in h.screen_buttons()
            if (data or "").startswith("aw_template:") and not label.startswith("✅")
        ]
        self.assertEqual(len(personal), 5, "пять личных шаблонов на страницу")
        nav = _wizard_nav(h, "aw_tplpage:")
        self.assertEqual([label for label, _data in nav], ["1/2", "▶️"])
        self.assertEqual(nav[0][1], "noop:1:2", "индикатор первой страницы")
        self.assertIn("1. Личный шаблон 1", h.screen_text())

        next_page = nav[-1][1]
        assert next_page is not None
        await h.callback(next_page)
        nav = _wizard_nav(h, "aw_tplpage:")
        self.assertEqual(
            [label for label, _data in nav],
            ["◀️", "2/2"],
            "последняя страница: ◀️ и индикатор",
        )
        self.assertEqual(nav[-1][1], "noop:2:2")
        self.assertIn("6. Личный шаблон 6", h.screen_text())
        second_page = [
            (label, data)
            for label, data in h.screen_buttons()
            if (data or "").startswith("aw_template:") and not label.startswith("✅")
        ]
        self.assertEqual(
            len(second_page),
            2,
            "на второй странице только оставшиеся два личных шаблона",
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
