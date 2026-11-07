# tests/test_stage25_list_context.py
"""
Poster 2.5 §15–16: контекст списков (plm_back, tp_pick_back, раскладка пикера).

Проверяется пользовательский контракт через настоящее
`Application.process_update`, FakeBot и временную SQLite:

- справочник «Управление → Площадки»: карточка, открытая со второй страницы
  списка, возвращает на ту же страницу; карточка из результатов поиска —
  в те же результаты с тем же запросом и страницей; после рестарта процесса
  «Назад» работает без stale-alert;
- выбор площадки для анонса: «Создать анонс → поиск → страница N →
  📝 Создать по личному шаблону → ◀️ Назад» возвращает те же результаты
  поиска той же страницы; без сессии шаблонов пикер открывается заново без
  stale-alert;
- под списком площадок `🔎 Найти площадку` и `➕ Новая площадка` стоят
  ОДНОЙ строкой (пустой список, непустой список, результаты поиска), затем
  `📝 Создать по личному шаблону`, последняя строка — возврат.

Живой Telegram, рабочая `post_bot.db` и сеть не используются.

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage25_list_context -v
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from place_state import load_place_session
from place_store import create_place
from template_state import load_template_session
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

BACK_LABEL = "◀️ Назад"
LIST_LABEL = "◀️ К списку"
SEARCH_LABEL = "🔎 Найти площадку"
NEW_PLACE_LABEL = "➕ Новая площадка"
TEMPLATE_LABEL = "📝 Создать по личному шаблону"


def _rows(h: Harness) -> list[list[tuple[str, str | None]]]:
    """Матрица inline-клавиатуры текущего экрана."""
    record = h.screen()
    if record is None:
        return []
    markup = record.get("reply_markup")
    if markup is None:
        return []
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


def _flat(h: Harness) -> list[tuple[str, str | None]]:
    return [item for row in _rows(h) for item in row]


def _callbacks(h: Harness, prefix: str = "") -> list[str]:
    return [
        data
        for _label, data in _flat(h)
        if data is not None and data.startswith(prefix)
    ]


def _alerts(h: Harness) -> list[str]:
    return h.answer_texts()


def _seed_places(h: Harness, count: int, *, prefix: str = "Зал страниц") -> list[int]:
    with h.session_factory() as session:
        user = get_or_create_user(session, DEFAULT_USER_ID, username="u")
        place_ids = []
        for index in range(count):
            place = create_place(
                session,
                f"{prefix} {index + 1}",
                city="Тестовый город",
                created_by_user_id=user.id,
            )
            place_ids.append(int(place.id))
    return place_ids


def _seed_personal_template(h: Harness, *, name: str = "Личный шаблон") -> int:
    from template_store import create_personal_template

    with h.session_factory() as session:
        get_or_create_user(session, DEFAULT_USER_ID, username="u")
        template = create_personal_template(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            name=name,
            content="[название]",
        )
        session.commit()
        return int(template.template_id)


def superadmin():
    """Супер­администратор только на время блока (живая конфигурация)."""
    return patch("config.SUPERADMIN_USER_IDS", (DEFAULT_USER_ID,))


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class ManageListContextTests(unittest.TestCase):
    """§15: `plm_back` восстанавливает страницу и запрос источника."""

    @scenario_test
    async def test_back_from_card_restores_list_page(self, h: Harness) -> None:
        _seed_places(h, 7)
        with superadmin():
            await h.text("/start")
            await h.callback("manage_places")
            await h.callback("plm_page:5")

            page_two = _callbacks(h, "plm_open:")
            self.assertTrue(page_two, "на второй странице нет площадок")
            await h.callback(page_two[-1])
            self.assertIn("🛡 Место", h.screen_text())

            await h.callback("adm_places")

        state = load_place_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertEqual(state["phase"], "manage_list")
        self.assertEqual(state.get("page"), 5, "страница списка не восстановлена")
        self.assertIn("Зал страниц 7", h.screen_text())
        self.assertIn(("2/2", "noop:2:2"), h.screen_buttons())

    @scenario_test
    async def test_back_from_search_card_restores_query_and_page(
        self, h: Harness
    ) -> None:
        _seed_places(h, 7)
        with superadmin():
            await h.text("/start")
            await h.callback("manage_places")
            await h.callback("plm_search")
            await h.text("Зал")
            self.assertEqual(
                _callbacks(h, "plm_spage:"),
                ["plm_spage:5"],
                "в результатах поиска нет второй страницы",
            )
            await h.callback("plm_spage:5")

            results = _callbacks(h, "plm_open:")
            self.assertTrue(results, "на второй странице поиска нет площадок")
            await h.callback(results[-1])
            self.assertIn("🛡 Место", h.screen_text())

            await h.callback("adm_places")

        state = load_place_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertEqual(state["phase"], "manage_search_results")
        self.assertEqual(state.get("query"), "Зал", "запрос поиска потерян")
        self.assertEqual(state.get("page"), 5, "страница поиска не восстановлена")
        self.assertIn("Результаты поиска: «Зал»", h.screen_text())
        self.assertIn(("2/2", "noop:2:2"), h.screen_buttons())

    @scenario_test
    async def test_back_from_view_card_restores_search(self, h: Harness) -> None:
        """Карточка просмотра (не-управляющий): `plm_back` → тот же поиск."""
        with h.session_factory() as session:
            other = get_or_create_user(session, 8_810_999, username="other")
            for index in range(6):
                create_place(
                    session,
                    f"Чужой зал {index + 1}",
                    city="Тестовый город",
                    created_by_user_id=other.id,
                )

        await h.text("/start")
        await h.callback("manage_places")
        await h.callback("plm_search")
        await h.text("Чужой")
        await h.callback("plm_spage:5")
        results = _callbacks(h, "plm_open:")
        self.assertTrue(results)
        await h.callback(results[-1])
        cat.assert_shows(
            self, h.screen_text(), "places.manage_card.eto_prosmotr_spravochnika"
        )

        await h.callback("plm_back")

        cat.assert_not_stale(
            self, " ".join(_alerts(h)), "ложный stale-alert на живом возврате"
        )
        state = load_place_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertEqual(state["phase"], "manage_search_results")
        self.assertEqual(state.get("query"), "Чужой")
        self.assertEqual(state.get("page"), 5)

    @scenario_test
    async def test_view_card_back_after_restart_has_no_stale_alert(
        self, h: Harness
    ) -> None:
        """Снимок `place_sessions` переживает рестарт: поиск и страница целы."""
        with h.session_factory() as session:
            other = get_or_create_user(session, 8_810_998, username="other2")
            for index in range(6):
                create_place(
                    session,
                    f"Другой зал {index + 1}",
                    city="Тестовый город",
                    created_by_user_id=other.id,
                )

        await h.text("/start")
        await h.callback("manage_places")
        await h.callback("plm_search")
        await h.text("Другой")
        await h.callback("plm_spage:5")
        results = _callbacks(h, "plm_open:")
        self.assertTrue(results)
        await h.callback(results[-1])

        await h.restart()

        answers_before = len(_alerts(h))
        await h.callback("plm_back")
        self.assertEqual(_alerts(h)[answers_before:], [], "stale-alert после рестарта")
        state = load_place_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertEqual(state["phase"], "manage_search_results")
        self.assertEqual(state.get("query"), "Другой")
        self.assertEqual(state.get("page"), 5)

    @scenario_test
    async def test_back_from_card_after_restart_has_no_stale_alert(
        self, h: Harness
    ) -> None:
        _seed_places(h, 7)
        with superadmin():
            await h.text("/start")
            await h.callback("manage_places")
            await h.callback("plm_page:5")
            page_two = _callbacks(h, "plm_open:")
            self.assertTrue(page_two)
            await h.callback(page_two[-1])

            await h.restart()

            answers_before = len(_alerts(h))
            await h.callback("adm_places")
            new_alerts = _alerts(h)[answers_before:]
            self.assertEqual(new_alerts, [], "stale-alert после рестарта")

        state = load_place_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertEqual(state["phase"], "manage_list")
        self.assertEqual(state.get("page"), 5, "страница потеряна после рестарта")
        self.assertIn("Зал страниц 7", h.screen_text())


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class PickBackContextTests(unittest.TestCase):
    """§15–16: `tp_pick_back` возвращает тот же поиск и страницу пикера."""

    @scenario_test
    async def test_back_from_templates_restores_search_page(self, h: Harness) -> None:
        _seed_places(h, 7)
        _seed_personal_template(h)
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback("pl_pick_search")
        await h.text("Зал")
        await h.callback("pl_picksp:5")
        self.assertIn("Зал страниц 7", h.screen_text())

        await h.callback("pick_template")
        self.assertIn(("◀️ Назад", "tp_pick_back"), _flat(h))
        answers_before = len(_alerts(h))

        await h.callback("tp_pick_back")

        self.assertEqual(_alerts(h)[answers_before:], [], "возврат показал stale-alert")
        state = load_place_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertEqual(state["phase"], "pick_search_results")
        self.assertEqual(state.get("query"), "Зал", "запрос пикера потерян")
        self.assertEqual(state.get("page"), 5, "страница поиска пикера потеряна")
        self.assertIn("Зал страниц 7", h.screen_text())
        self.assertIn(("2/2", "noop:2:2"), h.screen_buttons())
        self.assertIsNone(load_template_session(DEFAULT_USER_ID))

    @scenario_test
    async def test_back_from_templates_restores_list_page(self, h: Harness) -> None:
        _seed_places(h, 7)
        _seed_personal_template(h)
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback("pl_pickp:5")

        await h.callback("pick_template")
        await h.callback("tp_pick_back")

        state = load_place_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertEqual(state["phase"], "pick")
        self.assertEqual(state.get("pick_page"), 5)
        self.assertIn("Зал страниц 7", h.screen_text())

    @scenario_test
    async def test_back_without_template_session_opens_picker(self, h: Harness) -> None:
        _seed_places(h, 2)
        await h.text("/start")
        answers_before = len(_alerts(h))

        # Сессия шаблонов потеряна (рестарт/старый снимок): возврат не должен
        # упираться в stale-alert — открывается рабочий пикер площадок.
        await h.callback("tp_pick_back")

        self.assertEqual(_alerts(h)[answers_before:], [], "ложный stale-alert")
        state = load_place_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertEqual(state["phase"], "pick")
        self.assertIn("Зал страниц 1", h.screen_text())


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class PlacePickerLayoutTests(unittest.TestCase):
    """§16: `Найти` | `Новая площадка` одной строкой, шаблон, возврат."""

    def _assert_layout(self, h: Harness, *, last_callback: str) -> None:
        rows = _rows(h)
        self.assertTrue(rows, "экран без клавиатуры")
        self.assertIn(
            [(SEARCH_LABEL, "pl_pick_search"), (NEW_PLACE_LABEL, "pl_pick_add")],
            rows,
            f"поиск и создание не одной строкой: {rows}",
        )
        search_row = rows.index(
            [(SEARCH_LABEL, "pl_pick_search"), (NEW_PLACE_LABEL, "pl_pick_add")]
        )
        self.assertIn([(TEMPLATE_LABEL, "pick_template")], rows)
        template_row = rows.index([(TEMPLATE_LABEL, "pick_template")])
        self.assertGreater(template_row, search_row, "шаблон выше поиска")
        self.assertEqual(
            rows[-1][-1][1],
            last_callback,
            f"последняя строка не возврат: {rows[-1]}",
        )

    @scenario_test
    async def test_empty_picker_layout(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("pick_place")
        self._assert_layout(h, last_callback="main_menu")
        cat.assert_shows(self, h.screen_text(), "places.pick.empty")

    @scenario_test
    async def test_filled_picker_layout(self, h: Harness) -> None:
        _seed_places(h, 7)
        await h.text("/start")
        await h.callback("pick_place")
        self._assert_layout(h, last_callback="main_menu")
        self.assertIn(("1/2", "noop:1:2"), h.screen_buttons())

    @scenario_test
    async def test_search_results_layout(self, h: Harness) -> None:
        _seed_places(h, 7)
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback("pl_pick_search")
        await h.text("Зал")
        self._assert_layout(h, last_callback="pl_pick_back")

    @scenario_test
    async def test_empty_search_results_layout(self, h: Harness) -> None:
        _seed_places(h, 2)
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback("pl_pick_search")
        await h.text("нет-такого-места")
        self._assert_layout(h, last_callback="pl_pick_back")

    @scenario_test
    async def test_new_place_from_picker_back_returns_to_picker(
        self, h: Harness
    ) -> None:
        """§16: контекст исходного действия сохраняется при создании площадки."""
        _seed_places(h, 7)
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback("pl_pick_search")
        await h.text("Зал")
        await h.callback("pl_picksp:5")

        await h.callback("pl_pick_add")
        state = load_place_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertTrue(state.get("pick_mode"), "автозапуск анонса потерян")
        self.assertEqual(state.get("return_phase"), "pick_search_results")
        self.assertEqual(state.get("return_query"), "Зал")
        self.assertEqual(state.get("return_page"), 5)

        back = [
            data
            for _label, data in h.screen_buttons()
            if data in {"pl_back", "plm_back", "pl_create_back"}
        ]
        self.assertEqual(len(back), 1, f"нет однозначной кнопки «Назад»: {back}")
        await h.callback(back[0])
        state = load_place_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertEqual(state["phase"], "pick_search_results")
        self.assertEqual(state.get("query"), "Зал")
        self.assertEqual(state.get("page"), 5)
        self.assertIn("Зал страниц 7", h.screen_text())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
