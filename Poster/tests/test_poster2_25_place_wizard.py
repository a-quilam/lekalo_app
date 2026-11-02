# tests/test_poster2_25_place_wizard.py
"""
Poster 2.5 (A2) — мастер создания площадки: название → город → адрес → ссылка.

Что закрепляется:

(а) порядок шагов: название (обязательно, существующая дубль-проверка и
    подтверждение названия сохранены), город (обязательно), адрес и ссылка
    (необязательно);
(б) на обязательных шагах мастер принимает значение или возвращает на
    предыдущий шаг этого же мастера (`pl_create_back`); с шага названия
    «Назад» по-прежнему выходит к точке входа;
(в) на необязательных шагах клавиатура `◀️ Назад` | `⏭ Пропустить`;
(г) при первом пропуске поля остаются NULL — никаких заглушек;
(д) при возвращении к заполненному необязательному полю показано текущее
    значение, а «Пропустить» его НЕ стирает;
(е) после последнего шага площадка сохраняется сразу, без экрана
    подтверждения; поведение по режиму входа сохраняется: pick_mode
    запускает анонс, manage_mode открывает административную карточку с
    notice (а не промежуточное уведомление), обычный вход — карточку места;
(ж) некорректная ссылка оставляет пользователя на шаге, сообщение не
    удаляется; при успешном вводе/пропуске ввод удаляется (cleanup_ids).

Тесты идут через настоящее `Application.process_update`, FakeBot и
временную SQLite (`tests.integration_harness`): реальные Telegram, `.env` и
`post_bot.db` не используются.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

try:
    from sqlalchemy import select

    from models import Place, PlaceName
    from place_state import load_place_session
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        Harness,
        scenario_test,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)

ADDRESS_PROMPT = "Адрес площадки"
URL_PROMPT = "Ссылка площадки"
CITY_PROMPT = "В каком городе площадка"
BACK_LABEL = "◀️ Назад"
SKIP_LABEL = "⏭ Пропустить"


def _callbacks(h: Harness, prefix: str = "") -> list[str]:
    return [
        data
        for _label, data in h.screen_buttons()
        if data is not None and data.startswith(prefix)
    ]


def _labels(h: Harness) -> list[str]:
    return [label for label, _data in h.screen_buttons()]


def _places(h: Harness) -> list[Place]:
    with h.session_factory() as session:
        return list(session.scalars(select(Place).order_by(Place.id)).all())


def _place_session(h: Harness) -> dict:
    state = load_place_session(DEFAULT_USER_ID)
    assert state is not None
    return state


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CreateWizardOrderTests(unittest.TestCase):
    """Порядок четырёх шагов и результат сохранения."""

    @scenario_test
    async def test_all_four_fields_are_saved_in_order(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("places")
        await h.callback("pl_add")
        await h.text("Дом культуры")

        # Название: дубль-проверка и подтверждение сохранены.
        self.assertIn("Проверьте, нет ли этого места", h.screen_text())
        await h.callback("pl_create_confirm")
        self.assertIn(CITY_PROMPT, h.screen_text())

        await h.text("Москва")
        self.assertIn(ADDRESS_PROMPT, h.screen_text())
        self.assertEqual(_labels(h)[-2:], [BACK_LABEL, SKIP_LABEL])

        await h.text("ул. Ленина, 1")
        self.assertIn(URL_PROMPT, h.screen_text())
        self.assertEqual(_labels(h)[-2:], [BACK_LABEL, SKIP_LABEL])

        await h.text("t.me/poster")
        places = _places(h)
        self.assertEqual(len(places), 1)
        place = places[0]
        self.assertEqual(place.city, "Москва")
        self.assertEqual(place.address, "ул. Ленина, 1")
        self.assertEqual(place.url, "https://t.me/poster")

        # Сразу карточка места с notice: экрана подтверждения нет.
        self.assertIn("Дом культуры", h.screen_text())
        self.assertIn("✅ Место добавлено", h.screen_text())
        self.assertEqual(_place_session(h)["phase"], "confirm")
        self.assertEqual([item.name for item in _names(h)], ["Дом культуры"])

    @scenario_test
    async def test_skipping_both_optional_fields_keeps_them_null(self, h) -> None:
        await h.text("/start")
        await h.callback("places")
        await h.callback("pl_add")
        await h.text("Без адреса и ссылки")
        await h.callback("pl_create_confirm")
        await h.text("Тверь")
        await h.callback("pl_create_skip")
        await h.callback("pl_create_skip")

        places = _places(h)
        self.assertEqual(len(places), 1)
        self.assertIsNone(places[0].address, "пропуск адреса не даёт заглушку")
        self.assertIsNone(places[0].url, "пропуск ссылки не даёт заглушку")
        self.assertEqual(places[0].city, "Тверь")

    @scenario_test
    async def test_skip_only_address_saves_link(self, h) -> None:
        await h.text("/start")
        await h.callback("places")
        await h.callback("pl_add")
        await h.text("Только ссылка")
        await h.callback("pl_create_confirm")
        await h.text("Казань")
        await h.callback("pl_create_skip")
        await h.text("example.com/info")

        place = _places(h)[0]
        self.assertIsNone(place.address)
        self.assertEqual(place.url, "https://example.com/info")


def _names(h: Harness) -> list[PlaceName]:
    with h.session_factory() as session:
        return list(session.scalars(select(PlaceName).order_by(PlaceName.id)).all())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CreateWizardNavigationTests(unittest.TestCase):
    """«Назад» внутри мастера и повторный показ заполненных полей."""

    @scenario_test
    async def test_back_from_city_returns_to_name_check(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("places")
        await h.callback("pl_add")
        await h.text("Место с возвратом")
        await h.callback("pl_create_confirm")
        self.assertIn("pl_create_back", _callbacks(h))
        await h.callback("pl_create_back")

        self.assertIn("Проверьте, нет ли этого места", h.screen_text())
        self.assertIn("Место с возвратом", h.screen_text())
        self.assertEqual(_places(h), [])
        self.assertEqual(_place_session(h)["phase"], "create_check")

    @scenario_test
    async def test_back_from_address_keeps_city_and_returns_to_city(self, h) -> None:
        await h.text("/start")
        await h.callback("places")
        await h.callback("pl_add")
        await h.text("Место с городом")
        await h.callback("pl_create_confirm")
        await h.text("Самара")
        self.assertIn(ADDRESS_PROMPT, h.screen_text())

        await h.callback("pl_create_back")
        self.assertIn(CITY_PROMPT, h.screen_text())
        self.assertIn("Самара", h.screen_text(), "текущий город не показан")
        self.assertEqual(_place_session(h)["phase"], "create_city_input")

        # Повторный ввод города заменяет значение, а не создаёт вторую площадку.
        await h.text("Уфа")
        self.assertIn(ADDRESS_PROMPT, h.screen_text())
        await h.callback("pl_create_skip")
        await h.callback("pl_create_skip")
        places = _places(h)
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0].city, "Уфа")

    @scenario_test
    async def test_filled_address_is_shown_and_skip_does_not_erase_it(
        self, h: Harness
    ) -> None:
        await h.text("/start")
        await h.callback("places")
        await h.callback("pl_add")
        await h.text("Место с адресом")
        await h.callback("pl_create_confirm")
        await h.text("Пермь")
        await h.text("ул. Мира, 5")
        self.assertIn(URL_PROMPT, h.screen_text())

        # Возврат к заполненному адресу: текущее значение видно.
        await h.callback("pl_create_back")
        self.assertIn(ADDRESS_PROMPT, h.screen_text())
        self.assertIn("ул. Мира, 5", h.screen_text())
        self.assertEqual(_place_session(h)["address"], "ул. Мира, 5")

        # «Пропустить» НЕ стирает адрес.
        await h.callback("pl_create_skip")
        self.assertIn(URL_PROMPT, h.screen_text())
        self.assertEqual(
            _place_session(h)["address"], "ул. Мира, 5", "пропуск стёр адрес"
        )
        await h.callback("pl_create_skip")

        place = _places(h)[0]
        self.assertEqual(place.address, "ул. Мира, 5")

    @scenario_test
    async def test_new_address_replaces_previous_value(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("places")
        await h.callback("pl_add")
        await h.text("Место со сменой адреса")
        await h.callback("pl_create_confirm")
        await h.text("Омск")
        await h.text("Старый адрес")
        await h.callback("pl_create_back")
        await h.text("Новый адрес")
        await h.callback("pl_create_skip")

        self.assertEqual(_places(h)[0].address, "Новый адрес")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CreateWizardValidationTests(unittest.TestCase):
    """Некорректная ссылка: отказ, шаг не меняется, сообщение не удалено."""

    @scenario_test
    async def test_invalid_url_stays_on_step_and_keeps_input(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("places")
        await h.callback("pl_add")
        await h.text("Место с плохой ссылкой")
        await h.callback("pl_create_confirm")
        await h.text("Новосибирск")
        await h.callback("pl_create_skip")

        bad = await h.text("javascript:alert(1)")
        text = h.screen_text()
        self.assertIn(URL_PROMPT, text)
        self.assertIn("Не удалось распознать ссылку", text)
        self.assertEqual(_places(h), [], "площадка не должна создаваться")
        self.assertEqual(_place_session(h)["phase"], "create_url_input")
        self.assertNotIn(
            bad.message.message_id,
            h.deleted_ids(),
            "неудачный ввод не должен удаляться до исправления",
        )

        # Повторная попытка с валидной ссылкой завершает мастер.
        fixed = await h.text("https://t.me/poster")
        self.assertEqual(_places(h)[0].url, "https://t.me/poster")
        self.assertIn(fixed.message.message_id, h.deleted_ids())

    @scenario_test
    async def test_empty_address_suggests_skip_and_keeps_step(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("places")
        await h.callback("pl_add")
        await h.text("Место с пустым адресом")
        await h.callback("pl_create_confirm")
        await h.text("Курск")

        bad = await h.text("   ")
        text = h.screen_text()
        self.assertIn(ADDRESS_PROMPT, text)
        self.assertIn("⏭ Пропустить", text)
        self.assertNotIn("Удалить адрес", text)
        self.assertEqual(_places(h), [])
        self.assertNotIn(bad.message.message_id, h.deleted_ids())

        # Пропуск разрешает шаг: ошибочный ввод больше не нужен.
        await h.callback("pl_create_skip")
        self.assertIn(URL_PROMPT, h.screen_text())
        self.assertIn(bad.message.message_id, h.deleted_ids())

    @scenario_test
    async def test_empty_city_and_title_still_required(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("places")
        await h.callback("pl_add")
        await h.text("   ")
        self.assertIn("Название места не может быть пустым", h.screen_text())
        await h.text("Нормальное название")
        await h.callback("pl_create_confirm")
        await h.text("   ")
        self.assertIn("Город не может быть пустым", h.screen_text())
        self.assertEqual(_places(h), [])
        self.assertEqual(_place_session(h)["phase"], "create_city_input")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CreateWizardEntryModeTests(unittest.TestCase):
    """Завершение мастера по режиму входа: pick / manage / обычный."""

    @scenario_test
    async def test_pick_mode_saves_and_starts_announcement(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback("pl_pick_add")
        await h.text("Площадка из пикера")
        await h.callback("pl_create_confirm")
        await h.text("Владимир")
        await h.text("ул. Строителей, 7")
        await h.text("https://t.me/pick_place")

        places = _places(h)
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0].address, "ул. Строителей, 7")
        self.assertEqual(places[0].url, "https://t.me/pick_place")
        # Автозапуск анонса по стандартному шаблону сохранён.
        self.assertIn("Вопрос 1", h.screen_text())

    @scenario_test
    async def test_manage_mode_opens_admin_card_with_notice(self, h: Harness) -> None:
        with patch("config.SUPERADMIN_USER_IDS", (DEFAULT_USER_ID,)):
            await h.text("/start")
            await h.callback("manage_places")
            await h.callback("plm_add")
            await h.text("Площадка из справочника")
            await h.callback("pl_create_confirm")
            await h.text("Иркутск")
            await h.text("ул. Байкальская, 2")
            await h.text("https://example.com/irk")

        text = h.screen_text()
        self.assertIn("Площадка из справочника", text)
        self.assertIn("✅ Место и его стандартный шаблон добавлены", text)
        state = _place_session(h)
        self.assertEqual(state["phase"], "manage_card")
        place = _places(h)[0]
        self.assertEqual(state.get("place_id"), place.id)
        self.assertEqual(place.address, "ул. Байкальская, 2")
        self.assertEqual(place.url, "https://example.com/irk")

    @scenario_test
    async def test_regular_mode_opens_place_card(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("places")
        await h.callback("pl_add")
        await h.text("Обычная площадка")
        await h.callback("pl_create_confirm")
        await h.text("Тула")
        await h.text("ул. Оружейная, 1")
        await h.text("https://tula.example")

        self.assertIn("✅ Место добавлено", h.screen_text())
        self.assertIn("ул. Оружейная, 1", h.screen_text())
        state = _place_session(h)
        self.assertEqual(state["phase"], "confirm")
        self.assertEqual(state.get("place_id"), _places(h)[0].id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
