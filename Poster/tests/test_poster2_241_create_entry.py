# tests/test_poster2_241_create_entry.py
"""
Poster 2.4.1 T3 — единый вход «Создать анонс» (интеграционные сценарии).

До T3 в главном меню было пять кнопок и два параллельных входа в анонс:
«📝 Создать по личному шаблону» стоял рядом с «Создать для площадки».
После T3 вход один:

- главное меню — три строки и четыре кнопки:
  [📝 Создать анонс] / [📂 Мои анонсы] / [⚙️ Управление | ❓ Помощь];
- «📝 Создать анонс» (callback `pick_place`) открывает существующий экран
  выбора площадки, без промежуточных меню;
- в этом экране есть «📝 Создать по личному шаблону» (`pick_template`) и
  «◀️ Назад» (`main_menu`);
- «◀️ Назад» из пикера личных шаблонов (`tp_pick_back`) возвращает в выбор
  площадок и по возможности сохраняет страницу списка площадок.

Все сценарии идут через настоящее приложение (`bot.build_application`),
FakeBot и временную SQLite (`tests.integration_harness`): рабочая
`post_bot.db`, `.env` и сеть Telegram не используются.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

try:
    from announcement_store import list_announcement_drafts
    from handlers import places as places_module
    from handlers import templates as templates_module
    from place_state import load_place_session
    from place_store import create_place
    from template_state import load_template_session
    from template_store import create_personal_template
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(exc)

# Контракт главного меню: ровно три строки, подписи и callbacks.
MENU_ROWS = [
    [("📝 Создать анонс", "pick_place")],
    [("📂 Мои анонсы", "my_announcements")],
    [("⚙️ Управление", "manage"), ("❓ Помощь", "help")],
]
MENU_LABELS = [label for row in MENU_ROWS for label, _data in row]
MENU_CALLBACKS = [data for row in MENU_ROWS for _label, data in row]

TEMPLATE_PICK_LABEL = "📝 Создать по личному шаблону"
BACK_LABEL = "◀️ Назад"
NEW_PLACE_CALLBACK = "pl_pick_add"
PICK_PAGE_SIZE = places_module.PICK_PAGE_SIZE


# --- Помощники ---------------------------------------------------------------


def _rows(h) -> list[list[tuple[str, str | None]]]:
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


def _flat(h) -> list[tuple[str, str | None]]:
    return [item for row in _rows(h) for item in row]


def _callbacks(h, prefix: str = "") -> list[str]:
    return [
        data
        for _label, data in _flat(h)
        if data is not None and data.startswith(prefix)
    ]


def _rich_plain_text(payload: dict) -> str:
    """Весь текст Rich Message одним куском: заголовки, абзацы, списки."""
    parts: list[str] = []

    def walk(block: dict) -> None:
        kind = block.get("type")
        if kind == "section_heading":
            parts.append(block.get("heading", ""))
        elif kind == "paragraph":
            parts.append(block.get("text", ""))
        elif kind == "list":
            parts.extend(block.get("items") or ())
        elif kind == "details":
            parts.append(block.get("summary", ""))
            for child in block.get("blocks") or ():
                walk(child)

    for block in payload.get("blocks") or ():
        walk(block)
    return "\n".join(parts)


def _callback(h, prefix: str) -> str:
    found = _callbacks(h, prefix)
    if not found:
        raise AssertionError(f"нет callback {prefix!r}; есть: {_flat(h)}")
    return found[0]


def _seed_place(h, *, name: str = "Дом культуры") -> int:
    with h.session_factory() as session:
        user = get_or_create_user(session, DEFAULT_USER_ID, username="user")
        place = create_place(
            session,
            name,
            city="Тестовый город",
            created_by_user_id=user.id,
        )
        session.commit()
        return int(place.id)


def _seed_personal_template(h, *, name: str = "Личный шаблон") -> int:
    with h.session_factory() as session:
        get_or_create_user(session, DEFAULT_USER_ID, username="user")
        template = create_personal_template(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            name=name,
            content="[название]",
        )
        session.commit()
        return template.template_id


def _place_session(h) -> dict | None:
    return load_place_session(DEFAULT_USER_ID)


def _template_session(h) -> dict | None:
    return load_template_session(DEFAULT_USER_ID)


def _draft_count(h) -> int:
    with h.session_factory() as session:
        return len(
            list_announcement_drafts(session, actor_telegram_user_id=DEFAULT_USER_ID)
        )


# --- A. Главное меню --------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class MainMenuEntryTests(unittest.TestCase):
    """A: главное меню — три строки и ровно четыре кнопки."""

    @scenario_test
    async def test_menu_is_three_rows_with_single_create_entry(self, h):
        await h.text("/start")
        rows = _rows(h)
        self.assertEqual(rows, MENU_ROWS, f"меню не по контракту: {rows}")
        self.assertEqual(len(_flat(h)), 4, "в меню должно быть ровно 4 кнопки")

        # Старого второго входа в анонс в меню больше нет.
        self.assertNotIn("pick_template", _callbacks(h))
        for legacy in ("aw_start", "create_post", "drafts", "places", "templates"):
            self.assertNotIn(legacy, _callbacks(h))

    @scenario_test
    async def test_help_describes_single_entry(self, h):
        """Poster 2.5: справка — Rich Message; единый вход описан в разделах."""
        await h.text("/start")
        await h.callback("help")
        record = h.bot.rich_records[0]
        text = _rich_plain_text(record["rich_message"])
        self.assertIn("📝 Создать анонс", text)
        self.assertIn("Создать по личному шаблону", text)
        self.assertNotIn("📍 Создать для площадки", text)
        self.assertNotIn("пять кнопок", text.lower())


# --- B. Экран выбора площадки -----------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlacePickerEntryTests(unittest.TestCase):
    """B/C: `pick_place` — существующий пикер с новыми кнопками."""

    @scenario_test
    async def test_pick_place_opens_place_list(self, h):
        place_id = _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback("pick_place")

        state = _place_session(h)
        self.assertIsNotNone(state, "сессия выбора площадки не создана")
        assert state is not None
        self.assertEqual(state["phase"], "pick")
        self.assertEqual(state.get("pick_page"), 0)
        self.assertIn("Дом культуры", h.screen_text())
        self.assertIn(f"pl_pick:{place_id}", _callbacks(h, "pl_pick:"))

    @scenario_test
    async def test_place_picker_has_template_and_back_buttons(self, h):
        _seed_place(h)
        await h.text("/start")
        await h.callback("pick_place")

        buttons = _flat(h)
        self.assertIn((TEMPLATE_PICK_LABEL, "pick_template"), buttons)
        self.assertIn((BACK_LABEL, "main_menu"), buttons)
        # «🏠 В меню» на экране больше нет.
        self.assertNotIn("main_menu", [label for label, _d in buttons if False])
        labels = [label for label, _data in buttons]
        self.assertNotIn("🏠 В меню", labels)
        # Поиск и добавление остаются на месте.
        self.assertIn("pl_pick_search", _callbacks(h))
        self.assertIn(NEW_PLACE_CALLBACK, _callbacks(h))

    @scenario_test
    async def test_back_from_place_picker_returns_to_main_menu(self, h):
        _seed_place(h)
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback(_callback(h, "main_menu"))
        self.assertIn("Что хотите сделать?", h.screen_text())
        self.assertEqual(_rows(h), MENU_ROWS)


# --- D. Пикер личных шаблонов ------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TemplatePickerFromPlaceTests(unittest.TestCase):
    """D/E/F: `pick_template` из пикера площадок и возврат обратно."""

    @scenario_test
    async def test_pick_template_opens_personal_template_picker(self, h):
        template_id = _seed_personal_template(h, name="Мой шаблон")
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback("pick_template")

        state = _template_session(h)
        self.assertIsNotNone(state, "сессия шаблонов не создана")
        assert state is not None
        self.assertEqual(state["phase"], "pick")
        self.assertIn(f"tp_pick:{template_id}", _callbacks(h, "tp_pick:"))
        self.assertIn(("◀️ Назад", "tp_pick_back"), _flat(h))

    @scenario_test
    async def test_back_restores_place_page(self, h):
        # Больше одной страницы площадок: проверяем сохранение позиции.
        for number in range(1, PICK_PAGE_SIZE + 2):
            _seed_place(h, name=f"Площадка {number}")
        await h.text("/start")
        await h.callback("pick_place")

        second_page = _callback(h, "pl_pickp:")
        self.assertEqual(second_page, f"pl_pickp:{PICK_PAGE_SIZE}")
        await h.callback(second_page)
        self.assertEqual(_place_session(h).get("pick_page"), PICK_PAGE_SIZE)

        await h.callback("pick_template")
        await h.callback("tp_pick_back")

        state = _place_session(h)
        self.assertIsNotNone(state, "возврат не восстановил выбор площадки")
        assert state is not None
        self.assertEqual(state["phase"], "pick")
        self.assertEqual(
            state.get("pick_page"),
            PICK_PAGE_SIZE,
            "страница выбора площадок не сохранилась",
        )
        # На экране действительно вторая страница списка.
        self.assertIn(f"Площадка {PICK_PAGE_SIZE + 1}", h.screen_text())
        self.assertNotIn("Площадка 1\n", h.screen_text())
        # Сессия шаблонов очищена: это экран выбора площадки, а не шаблонов.
        self.assertIsNone(_template_session(h))

    @scenario_test
    async def test_back_from_empty_template_picker_goes_to_places(self, h):
        _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback("pick_place")
        with patch.object(templates_module, "list_personal_templates", return_value=[]):
            await h.callback("pick_template")
            text = h.screen_text()
            self.assertIn("Личных шаблонов пока нет", text)
            # Пустое состояние: «Назад» ведёт в выбор площадок, не в меню.
            buttons = _flat(h)
            self.assertIn((BACK_LABEL, "tp_pick_back"), buttons)
            self.assertNotIn("main_menu", [data for _l, data in buttons])
            await h.callback("tp_pick_back")

        state = _place_session(h)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "pick")
        self.assertIn("Дом культуры", h.screen_text())


# --- G/H. Существующее поведение выбора площадки -----------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlacePickerBehaviourTests(unittest.TestCase):
    """G/H: тап по месту, поиск, «К списку» и пустой список не сломаны."""

    @scenario_test
    async def test_place_tap_starts_announcement(self, h):
        place_id = _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback(f"pl_pick:{place_id}")

        self.assertIn("Вопрос 1", h.screen_text())
        self.assertEqual(_draft_count(h), 1, "тап по месту не создал черновик")

    @scenario_test
    async def test_search_and_back_to_list(self, h):
        _seed_place(h, name="Дом культуры")
        _seed_place(h, name="Театр Этаж")
        await h.text("/start")
        await h.callback("pick_place")

        await h.callback("pl_pick_search")
        await h.text("Дом")
        search_text = h.screen_text()
        self.assertIn("Дом культуры", search_text)
        self.assertNotIn("Театр Этаж", search_text)
        # В результатах есть и личный шаблон, и «К списку».
        self.assertIn("pick_template", _callbacks(h))
        self.assertIn("pl_pick_back", _callbacks(h))

        await h.callback("pl_pick_back")
        self.assertIn("Театр Этаж", h.screen_text())
        self.assertEqual(_place_session(h)["phase"], "pick")

    @scenario_test
    async def test_empty_place_list_has_new_place_and_back(self, h):
        await h.text("/start")
        await h.callback("pick_place")

        buttons = _flat(h)
        self.assertIn(
            ("➕ Новая площадка", NEW_PLACE_CALLBACK),
            buttons,
            f"нет «Новая площадка»: {buttons}",
        )
        self.assertIn((BACK_LABEL, "main_menu"), buttons)
        self.assertIn((TEMPLATE_PICK_LABEL, "pick_template"), buttons)
        self.assertNotIn("🏠 В меню", [label for label, _data in buttons])


if __name__ == "__main__":
    unittest.main()
