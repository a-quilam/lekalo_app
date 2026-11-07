# tests/test_stage25_acceptance_a1a2.py
"""
Poster 2.5, независимая приёмка A1+A2 (Tester).

Проверяется УТВЕРЖДЁННОЕ пользовательское поведение (spec §9, §10, §11,
§57 пп. 1–5), а не реализация Developer:

A1 — «⚙️ Управление»: ровно четыре строки (площадки / шаблоны+кирпичики /
администрирование по правам / в меню), прежние callback_data, 🧩 вместо 🧱,
индивидуальные эмодзи системных кирпичиков, отсутствие добавленного эмодзи
у пользовательских кирпичиков и неизменные коды `[код]`.

A2 — мастер создания площадки: название → подтверждение → город → адрес →
ссылка; пропуск необязательных полей даёт NULL; «Назад» редактирует тот же
служебный экран; заполненное поле показывается и «Пропустить» его не стирает;
некорректная ссылка не удаляет ввод; оба маршрута входа (pick/manage)
завершаются прежним поведением; битое состояние не ломает бота.

Тесты идут через настоящее `Application.process_update`, FakeBot и
временную SQLite (`tests.integration_harness`). Реальная база, сеть и
настоящий бот не используются.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import select

import place_state
from brick_store import create_personal_brick
from models import Place
from tests import ui_texts_expect as cat
from tests.integration_harness import (
    DEFAULT_USER_ID,
    HARNESS_IMPORT_ERROR,
    Harness,
    scenario_test,
)
from user_store import get_or_create_user

INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR

BRICK = "\U0001f9f1"  # 🧱 — выведен из пользовательских строк
PUZZLE = "\U0001f9e9"  # 🧩

SKIP_LABEL = "⏭ Пропустить"
BACK_LABEL = "◀️ Назад"

SYSTEM_EMOJIS = {
    "название": "🏷",
    "дата": "📅",
    "время_начала": "🕒",
    "время_окончания": "🕒",
    "место": "📍",
    "ссылка_места": "🔗",
    "описание": "📝",
    "контакт": "☎️",
    "адрес": "🗺",
    "город": "🌆",
}


# --- помощники ---------------------------------------------------------------


def _rows(h: Harness) -> list[list[tuple[str, str | None]]]:
    """Строки inline-клавиатуры текущего экрана: [(подпись, callback)]."""
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


def _places(h: Harness) -> list[Place]:
    with h.session_factory() as session:
        return list(session.scalars(select(Place).order_by(Place.id)).all())


def _place(h: Harness) -> Place:
    places = _places(h)
    assert len(places) == 1, f"ожидалась одна площадка, факт: {len(places)}"
    return places[0]


def _state(h: Harness) -> dict:
    state = place_state.load_place_session(DEFAULT_USER_ID)
    assert state is not None, "снимок состояния мастера площадки потерян"
    return state


async def _to_address(h: Harness, name: str, city: str):
    await h.text("/start")
    await h.callback("places")
    await h.callback("pl_add")
    await h.text(name)
    await h.callback("pl_create_confirm")
    await h.text(city)


# --- A1: интерфейс управления ------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ManageScreenAcceptanceTests(unittest.TestCase):
    """§10: компактное меню «Управление» и §9: единый эмодзи кирпичиков."""

    @scenario_test
    async def test_manage_screen_rows_and_callbacks_without_rights(
        self, h: Harness
    ) -> None:
        await h.text("/start")
        await h.callback("manage")
        rows = _rows(h)
        self.assertEqual(
            [[data for _label, data in row] for row in rows],
            [
                ["manage_places"],
                ["manage_templates", "manage_bricks"],
                ["main_menu"],
            ],
            f"фактическая раскладка «Управления»: {rows}",
        )
        self.assertEqual([label for label, _ in rows[0]], ["📍 Площадки"])
        self.assertEqual(
            [label for label, _ in rows[1]], ["📝 Шаблоны", "🧩 Кирпичики"]
        )
        self.assertEqual([label for label, _ in rows[2]], ["🏠 В меню"])
        # Пустых кнопок нет.
        for row in rows:
            self.assertTrue(row, "пустая строка кнопок")
            for label, data in row:
                self.assertTrue(label.strip(), "кнопка без подписи")
                self.assertIsNotNone(data, f"кнопка «{label}» без callback_data")

    @scenario_test
    async def test_manage_screen_shows_admin_only_with_rights(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("manage")
        self.assertNotIn(
            "admin_panel",
            [data for row in _rows(h) for _label, data in row],
            "администрирование видно без прав",
        )

        with patch("config.SUPERADMIN_USER_IDS", (DEFAULT_USER_ID,)):
            await h.callback("main_menu")
            await h.callback("manage")
            rows = _rows(h)
        self.assertEqual(
            [[data for _label, data in row] for row in rows],
            [
                ["manage_places"],
                ["manage_templates", "manage_bricks"],
                ["admin_panel"],
                ["main_menu"],
            ],
            f"администрирование — отдельной строкой над «В меню»: {rows}",
        )
        self.assertEqual([label for label, _ in rows[2]], ["🛡 Администрирование"])

    @scenario_test
    async def test_manage_shows_admin_for_place_admin(self, h: Harness) -> None:
        """§10: администрирование видно и администратору места, не только суперадмину."""
        from models import PlaceAdmin, utcnow
        from place_store import create_place

        with h.session_factory() as session:
            user = get_or_create_user(session, h.user_id, username="u")
            place = create_place(
                session, "Дом культуры", city="Москва", created_by_user_id=user.id
            )
            session.add(
                PlaceAdmin(
                    place_id=place.id,
                    user_id=user.id,
                    assigned_by_user_id=None,
                    assigned_at=utcnow(),
                )
            )
            session.commit()
        await h.text("/start")
        await h.callback("manage")
        data = [cb for row in _rows(h) for _label, cb in row]
        self.assertIn(
            "admin_panel", data, f"администратор места не видит раздел: {data}"
        )

    @scenario_test
    async def test_puzzle_emoji_in_brick_user_strings_without_old_brick(
        self, h: Harness
    ) -> None:
        with h.session_factory() as session:
            get_or_create_user(session, h.user_id, username="u")
            create_personal_brick(
                session,
                actor_telegram_user_id=h.user_id,
                code="цена",
                title="Цена",
                value_type="short_text",
            )
        await h.text("/start")
        await h.callback("manage")
        manage_labels = [label for label, _data in h.screen_buttons()]
        await h.callback("manage_bricks")
        hub_text = h.screen_text()
        hub_labels = [label for label, _data in h.screen_buttons()]
        await h.callback("bp_mine")
        proposals_text = h.screen_text()

        self.assertIn(
            "🧩 Кирпичики",
            manage_labels,
            f"в «Управлении» кнопка кирпичиков без 🧩: {manage_labels}",
        )
        self.assertIn("🧩", hub_text, "хаб кирпичиков без 🧩")
        self.assertIn("🧩", proposals_text, "предложения кирпичиков без 🧩")
        for text in (hub_text, proposals_text):
            self.assertNotIn(
                BRICK, text, f"в пользовательском тексте остался 🧱: {text[:150]}"
            )
        for label in manage_labels + hub_labels:
            self.assertNotIn(BRICK, label, f"в подписи кнопки остался 🧱: {label}")

    @scenario_test
    async def test_system_bricks_have_own_emoji_and_code_unchanged(
        self, h: Harness
    ) -> None:
        with patch("config.SUPERADMIN_USER_IDS", (DEFAULT_USER_ID,)):
            await h.text("/start")
            await h.callback("admin_panel")
            await h.callback("adm_bricks_global")
            # Poster 2.5 §14: пять записей на страницу — собираем ярлыки
            # со всех страниц, иначе проверка зависит от размера страницы.
            labels: dict[str, None] = {}
            seen_offsets: set[str] = set()
            while True:
                for label, _data in h.screen_buttons():
                    if label.startswith("["):
                        labels.setdefault(label, None)
                next_page = next(
                    (
                        data
                        for label, data in h.screen_buttons()
                        if data
                        and data.startswith("adm_bricks_page:")
                        and label == "▶️"
                    ),
                    None,
                )
                if next_page is None or next_page in seen_offsets:
                    break
                seen_offsets.add(next_page)
                await h.callback(next_page)
        all_labels = list(labels)

        for code, emoji in SYSTEM_EMOJIS.items():
            with self.subTest(code=code):
                matches = [
                    label for label in all_labels if label.startswith(f"[{code}] ")
                ]
                self.assertEqual(
                    len(matches),
                    1,
                    f"системный кирпичик [{code}] не найден: {all_labels}",
                )
                self.assertIn(
                    emoji,
                    matches[0],
                    f"у системного [{code}] нет эмодзи {emoji}: {matches[0]!r}",
                )
        # Коды не изменились: у каждого системного кирпичика свой `[код]`.
        codes = [
            label.split("]")[0][1:] for label in all_labels if label.startswith("[")
        ]
        self.assertEqual(sorted(codes), sorted(SYSTEM_EMOJIS))

    @scenario_test
    async def test_custom_brick_has_no_added_emoji(self, h: Harness) -> None:
        with h.session_factory() as session:
            get_or_create_user(session, h.user_id, username="u")
            create_personal_brick(
                session,
                actor_telegram_user_id=h.user_id,
                code="цена",
                title="Цена",
                value_type="short_text",
            )
        await h.text("/start")
        await h.callback("manage_bricks")
        labels = [label for label, _data in h.screen_buttons()]
        custom = [label for label in labels if "[цена]" in label]
        self.assertEqual(len(custom), 1, f"нет записи личного кирпичика: {labels}")
        self.assertEqual(
            custom[0].strip(),
            "Цена — [цена]",
            f"у пользовательского кирпичика добавлено оформление: {custom[0]!r}",
        )
        self.assertNotIn(
            "🏷", custom[0], "пользовательскому кирпичику присвоен системный эмодзи"
        )

    @scenario_test
    async def test_wizard_question_and_brick_card_use_emoji(self, h: Harness) -> None:
        with h.session_factory() as session:
            user = get_or_create_user(session, h.user_id, username="u")
            from place_store import create_place

            place = create_place(
                session, "Дом культуры", city="Москва", created_by_user_id=user.id
            )
            place_id = place.id
        with patch("config.SUPERADMIN_USER_IDS", (DEFAULT_USER_ID,)):
            await h.text("/start")
            await h.callback("admin_panel")
            await h.callback("adm_bricks_global")
            await h.callback("adm_bedit:2")  # системный кирпичик «дата»
            card = h.screen_text()
        self.assertIn(
            "📅", card, f"карточка системного кирпичика без эмодзи: {card[:120]}"
        )
        self.assertIn("[дата]", card, "код системного кирпичика изменился")

        await h.callback("main_menu")
        await h.callback("pick_place")
        await h.callback(f"pl_pick:{place_id}")
        question = h.screen_text()
        self.assertIn("🧩", question, "экран вопроса анонса без 🧩")
        self.assertIn("🏷", question, "вопрос о названии без эмодзи 🏷")
        self.assertNotIn(BRICK, question, "в вопросе анонса остался 🧱")


# --- A2: мастер создания площадки -------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CreateWizardFieldsAcceptanceTests(unittest.TestCase):
    """§11: четыре поля, NULL вместо заглушек, удаление принятого ввода."""

    @scenario_test
    async def test_all_four_fields_saved_and_inputs_removed(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("places")
        await h.callback("pl_add")
        name_msg = await h.text("Дом культуры")
        cat.assert_shows(self, h.screen_text(), "places.create_check.title")
        await h.callback("pl_create_confirm")
        self.assertIn("В каком городе площадка", h.screen_text())
        self.assertEqual(_places(h), [], "площадка создана до конца мастера")

        city_msg = await h.text("Москва")
        assert city_msg.message is not None
        self.assertIn("🗺 Адрес площадки", h.screen_text())
        self.assertEqual([label for label, _ in _rows(h)[-1]], [BACK_LABEL, SKIP_LABEL])
        self.assertEqual(_places(h), [])
        # Ввод принятого шага удаляется сразу, не в конце мастера.
        self.assertIn(
            city_msg.message.message_id,
            h.deleted_ids(),
            "город принят, но сообщение с городом осталось в чате",
        )
        sends_before = len([call for call in h.calls if call["type"] == "message"])

        address_msg = await h.text("ул. Ленина, 1")
        assert address_msg.message is not None
        self.assertIn("🔗 Ссылка площадки", h.screen_text())
        self.assertEqual([label for label, _ in _rows(h)[-1]], [BACK_LABEL, SKIP_LABEL])
        self.assertEqual(_places(h), [])
        self.assertIn(
            address_msg.message.message_id,
            h.deleted_ids(),
            "адрес принят, но сообщение с адресом осталось в чате",
        )
        self.assertEqual(
            len([call for call in h.calls if call["type"] == "message"]),
            sends_before,
            "переход к следующему шагу мастера создал новое сообщение бота",
        )

        url_msg = await h.text("t.me/poster")
        place = _place(h)
        self.assertEqual(place.city, "Москва")
        self.assertEqual(place.address, "ул. Ленина, 1")
        self.assertEqual(place.url, "https://t.me/poster")
        # После последнего шага — карточка места, без экрана подтверждения.
        cat.assert_shows(
            self,
            h.screen_text(),
            "places.create_place_from_state.mesto_dobavleno_ono_srazu",
        )
        self.assertIn("Дом культуры", h.screen_text())
        self.assertEqual(_state(h)["phase"], "confirm")

        deleted = set(h.deleted_ids())
        for message, label in (
            (name_msg, "название"),
            (city_msg, "город"),
            (address_msg, "адрес"),
            (url_msg, "ссылка"),
        ):
            with self.subTest(field=label):
                assert message.message is not None
                self.assertIn(
                    message.message.message_id,
                    deleted,
                    f"принятый ввод «{label}» не удалён после успешного шага",
                )

    @scenario_test
    async def test_skip_both_optional_fields_keeps_null(self, h: Harness) -> None:
        await _to_address(h, "Без адреса и ссылки", "Тверь")
        await h.callback("pl_create_skip")
        self.assertIn("🔗 Ссылка площадки", h.screen_text())
        await h.callback("pl_create_skip")

        place = _place(h)
        self.assertEqual(place.city, "Тверь")
        self.assertIsNone(place.address, "пропуск адреса оставил заглушку")
        self.assertIsNone(place.url, "пропуск ссылки оставил заглушку")
        cat.assert_shows(
            self,
            h.screen_text(),
            "places.create_place_from_state.mesto_dobavleno_ono_srazu",
        )

    @scenario_test
    async def test_skip_only_address_saves_link(self, h: Harness) -> None:
        await _to_address(h, "Только ссылка", "Казань")
        await h.callback("pl_create_skip")
        await h.text("example.com/info")
        place = _place(h)
        self.assertIsNone(place.address)
        self.assertEqual(place.url, "https://example.com/info")

    @scenario_test
    async def test_repeated_entry_shows_value_and_skip_does_not_erase(
        self, h: Harness
    ) -> None:
        await _to_address(h, "Место с адресом", "Пермь")
        await h.text("ул. Мира, 5")
        self.assertIn("🔗 Ссылка площадки", h.screen_text())

        await h.callback("pl_create_back")
        self.assertIn("🗺 Адрес площадки", h.screen_text())
        self.assertIn("ул. Мира, 5", h.screen_text(), "прежний адрес не показан")
        self.assertEqual(_state(h)["address"], "ул. Мира, 5")

        await h.callback("pl_create_skip")
        self.assertIn("🔗 Ссылка площадки", h.screen_text())
        self.assertEqual(_state(h)["address"], "ул. Мира, 5", "«Пропустить» стёр адрес")
        await h.callback("pl_create_skip")
        self.assertEqual(_place(h).address, "ул. Мира, 5")
        self.assertIsNone(_place(h).url)

    @scenario_test
    async def test_invalid_url_keeps_user_on_step_and_input(self, h: Harness) -> None:
        await _to_address(h, "Место с плохой ссылкой", "Новосибирск")
        await h.callback("pl_create_skip")

        bad = await h.text("javascript:alert(1)")
        assert bad.message is not None
        text = h.screen_text()
        self.assertIn("🔗 Ссылка площадки", text, "шаг сменился при ошибке")
        self.assertIn("Не удалось распознать ссылку", text, "нет понятной ошибки")
        self.assertEqual(_state(h)["phase"], "create_url_input")
        self.assertEqual(_places(h), [], "площадка создана с невалидной ссылкой")
        self.assertNotIn(
            bad.message.message_id,
            h.deleted_ids(),
            "сообщение с некорректной ссылкой удалено до исправления",
        )

        fixed = await h.text("https://t.me/poster")
        assert fixed.message is not None
        self.assertEqual(_place(h).url, "https://t.me/poster")
        self.assertIn(fixed.message.message_id, h.deleted_ids())

    @scenario_test
    async def test_invalid_url_variants_rejected(self, h: Harness) -> None:
        await _to_address(h, "Место со ссылками", "Курск")
        await h.callback("pl_create_skip")
        for bad in ("   ", "example .com", "ftp://example.com", "user@example.com"):
            with self.subTest(value=bad):
                await h.text(bad)
                self.assertIn("Не удалось распознать ссылку", h.screen_text())
                self.assertEqual(_state(h)["phase"], "create_url_input")
                self.assertEqual(_places(h), [])

        # Сокращённая запись принимается прежним валидатором.
        await h.text("@poster")
        self.assertEqual(_place(h).url, "https://t.me/poster")

    @scenario_test
    async def test_empty_title_and_city_still_required(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("places")
        await h.callback("pl_add")
        await h.text("   ")
        self.assertIn("Название места не может быть пустым", h.screen_text())
        self.assertEqual(_places(h), [])
        await h.text("Нормальное название")
        await h.callback("pl_create_confirm")
        await h.text("   ")
        self.assertIn("Город не может быть пустым", h.screen_text())
        self.assertEqual(_places(h), [])
        self.assertEqual(_state(h)["phase"], "create_city_input")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CreateWizardNavigationAcceptanceTests(unittest.TestCase):
    """§11/§57 п. 5: «Назад» внутри мастера, порядок и число сообщений."""

    @scenario_test
    async def test_back_between_steps_reuses_same_service_screen(
        self, h: Harness
    ) -> None:
        await _to_address(h, "Место с возвратом", "Самара")
        await h.text("ул. Садовая, 3")

        def send_count() -> int:
            return len([call for call in h.calls if call["type"] == "message"])

        sends_before = send_count()
        screen_ids: list[int | None] = [h.screen_message_id()]

        await h.callback("pl_create_back")
        self.assertIn("🗺 Адрес площадки", h.screen_text())
        screen_ids.append(h.screen_message_id())
        await h.callback("pl_create_back")
        self.assertIn("В каком городе площадка", h.screen_text())
        screen_ids.append(h.screen_message_id())
        await h.callback("pl_create_back")
        cat.assert_shows(self, h.screen_text(), "places.create_check.title")
        screen_ids.append(h.screen_message_id())
        self.assertIn("Место с возвратом", h.screen_text())

        self.assertEqual(
            send_count(),
            sends_before,
            "«Назад» создаёт новое сообщение бота вместо правки экрана",
        )
        self.assertEqual(
            len(set(screen_ids)),
            1,
            f"«Назад» пересоздаёт служебный экран: {screen_ids}",
        )
        self.assertEqual(_places(h), [], "«Назад» по мастеру создал площадку")

    @scenario_test
    async def test_back_from_address_keeps_city(self, h: Harness) -> None:
        await _to_address(h, "Место с городом", "Уфа")
        await h.callback("pl_create_back")
        self.assertIn("В каком городе площадка", h.screen_text())
        self.assertIn("Уфа", h.screen_text(), "текущий город не показан")
        await h.text("Омск")
        self.assertIn("🗺 Адрес площадки", h.screen_text())
        await h.callback("pl_create_skip")
        await h.callback("pl_create_skip")
        self.assertEqual(_place(h).city, "Омск")
        self.assertIsNone(_place(h).address)

    @scenario_test
    async def test_stale_skip_and_back_do_not_duplicate_place(self, h: Harness) -> None:
        await _to_address(h, "Место без дублей", "Тула")
        await h.callback("pl_create_skip")
        await h.callback("pl_create_skip")
        self.assertEqual(len(_places(h)), 1)

        answers_before = len(h.answers())
        await h.callback("pl_create_skip")  # устаревшая кнопка прошлого шага
        await h.callback("pl_create_back")
        self.assertEqual(len(_places(h)), 1, "устаревшая кнопка создала дубль")
        cat.assert_shows(
            self,
            h.screen_text(),
            "places.create_place_from_state.mesto_dobavleno_ono_srazu",
        )
        self.assertGreaterEqual(
            len(h.answers()), answers_before, "устаревшая кнопка не отвечает на нажатие"
        )

    @scenario_test
    async def test_broken_state_without_city_falls_back_safely(
        self, h: Harness
    ) -> None:
        """Битый снимок мастера (нет обязательного города) не ломает бота."""
        await _to_address(h, "Битое состояние", "Ижевск")
        state = _state(h)
        state.pop("city", None)
        place_state.save_place_session(DEFAULT_USER_ID, state)

        await h.callback("pl_create_skip")
        text = h.screen_text()
        self.assertIn("📍 Места", text, f"нет безопасного возврата: {text[:200]}")
        self.assertEqual(_places(h), [])
        await h.text("/start")  # бот жив после битого состояния
        self.assertIn("Здравствуйте", h.screen_text())

    @scenario_test
    async def test_survives_restart_mid_wizard(self, h: Harness) -> None:
        await _to_address(h, "После рестарта", "Москва")
        await h.text("ул. Первая")
        await h.restart()
        self.assertIn("🔗 Ссылка площадки", h.screen_text())
        await h.text("https://x.example")
        place = _place(h)
        self.assertEqual(place.address, "ул. Первая")
        self.assertEqual(place.url, "https://x.example")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CreateWizardEntryModeAcceptanceTests(unittest.TestCase):
    """§11: маршруты входа сохраняют прежнее завершение (pick/manage)."""

    @scenario_test
    async def test_pick_mode_starts_announcement_after_last_step(
        self, h: Harness
    ) -> None:
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback("pl_pick_add")
        await h.text("Площадка из пикера")
        await h.callback("pl_create_confirm")
        await h.text("Владимир")
        await h.text("ул. Строителей, 7")
        await h.text("t.me/pick_place")

        place = _place(h)
        self.assertEqual(place.address, "ул. Строителей, 7")
        self.assertEqual(place.url, "https://t.me/pick_place")
        self.assertRegex(
            h.screen_text(),
            cat.question_re(1),
            "из «Создать анонс» анонс не запустился по стандартному шаблону",
        )

    @scenario_test
    async def test_pick_mode_skips_optional_fields(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback("pl_pick_add")
        await h.text("Пикер без полей")
        await h.callback("pl_create_confirm")
        await h.text("Рязань")
        await h.callback("pl_create_skip")
        await h.callback("pl_create_skip")
        place = _place(h)
        self.assertIsNone(place.address)
        self.assertIsNone(place.url)
        self.assertRegex(h.screen_text(), cat.question_re(1))

    @scenario_test
    async def test_manage_mode_opens_admin_card(self, h: Harness) -> None:
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
        cat.assert_shows(
            self, text, "places.create_place_from_state.mesto_dobavleno_vmeste_standart"
        )
        self.assertEqual(_state(h)["phase"], "manage_card")
        self.assertEqual(_state(h)["place_id"], _place(h).id)
        self.assertEqual(_place(h).address, "ул. Байкальская, 2")
        self.assertEqual(_place(h).url, "https://example.com/irk")

    @scenario_test
    async def test_regular_mode_opens_place_card_with_all_values(
        self, h: Harness
    ) -> None:
        await _to_address(h, "Обычная площадка", "Тула")
        await h.text("<b>ул. Оружейная, 1</b>")
        await h.text("https://tula.example")
        text = h.screen_text()
        cat.assert_shows(
            self, text, "places.create_place_from_state.mesto_dobavleno_ono_srazu"
        )
        place = _place(h)
        self.assertEqual(place.address, "<b>ул. Оружейная, 1</b>")
        self.assertNotIn(
            "<b>",
            text,
            "пользовательский адрес не экранирован в HTML-экране карточки",
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class SystemEmojiCoverageDefectTests(unittest.TestCase):
    """
    ДЕФЕКТ A1-D1 (§9): в экранах шаблона системные кирпичики показаны без
    индивидуальных эмодзи, хотя в остальном интерфейсе (хаб, карточки,
    административный список, вопрос анонса) они уже с эмодзи.

    Воспроизведение: «Мои шаблоны» → карточка шаблона → «📋 Кирпичики»
    (список доступных кирпичиков).
    Ожидание: «• [дата] — 📅 Дата …», «• [название] — 🏷 Название
    мероприятия»; коды `[код]` не меняются.
    """

    async def _template_with_bindings(self, h: Harness) -> int:
        from template_store import create_personal_template

        with h.session_factory() as session:
            get_or_create_user(session, h.user_id, username="u")
            created = create_personal_template(
                session,
                actor_telegram_user_id=h.user_id,
                name="Мой шаблон",
                content="Шапка\n[название]\n[место]\n[дата]\nПодвал",
            )
        await h.text("/start")
        await h.text("/templates")
        return created.template_id

    @scenario_test
    async def test_template_brick_list_shows_system_emoji(self, h: Harness) -> None:
        template_id = await self._template_with_bindings(h)
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_bricks:{template_id}")
        # Poster 2.5 §14: пять записей на страницу — смотрим все страницы.
        listed: list[str] = []
        seen: set[str] = set()
        while True:
            listed.append(h.screen_text())
            next_page = next(
                (
                    data
                    for _label, data in h.screen_buttons()
                    if data and data.startswith("tp_bpage:")
                ),
                None,
            )
            if next_page is None or next_page in seen:
                break
            seen.add(next_page)
            await h.callback(next_page)
        listed_text = "\n".join(listed)
        for code, emoji in (
            ("название", "🏷"),
            ("место", "📍"),
            ("дата", "📅"),
        ):
            with self.subTest(code=code):
                self.assertIn(
                    f"[{code}] — {emoji}",
                    listed_text,
                    f"в списке кирпичиков шаблона [{code}] без эмодзи {emoji}",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
