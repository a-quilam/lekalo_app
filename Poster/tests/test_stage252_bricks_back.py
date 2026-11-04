# tests/test_stage252_bricks_back.py
"""
Poster 2.5.2, T2: «◀️ Назад» из хаба «Мои кирпичики» ведёт в источник входа.

Регрессия (сценарий 11): в хабе личных кирпичиков не было «Назад» — человек
мог выйти только в главное меню, а вход из «⚙️ Управление» терялся.

Что закрепляется:

- в хабе есть «◀️ Назад» (`bk_back`), и из «Управление → Кирпичики» он
  возвращает в «Управление», а не в главное меню;
- источник входа (`hub_origin`) хранится в `brick_sessions.payload` и
  переживает рестарт процесса: кнопка со старого сообщения после рестарта
  тоже ведёт в «Управление»;
- источник не теряется при заходе в мастер создания и в карточку кирпичика:
  после «Назад» из них хаб помнит вход, и следующий «Назад» выходит в
  «Управление»;
- вход с шага конструктора анонса (старый callback `bricks`) возвращает на
  тот же шаг из resume-снимка, а справка поверх хаба источник не сбрасывает;
- у старой сессии без `hub_origin` (прямая команда `/bricks`, старый
  callback) безопасный фолбэк — «Управление», без «внутренней ошибки»;
- кнопка «🏠 В меню» в хабе остаётся рабочей.

Только настоящее приложение, FakeBot и временная SQLite: реальные
`post_bot.db`, `.env` и сеть Telegram не используются.
"""

from __future__ import annotations

import json
import unittest

try:
    from brick_state import load_brick_session
    from brick_store import create_personal_brick
    from models import BrickSession
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


OWNER_TG = 2_160_801
MANAGE_TITLE = "⚙️ Управление"
BACK_LABEL = "◀️ Назад"
HUB_TITLE = "Мои кирпичики"


def _buttons(h) -> list[tuple[str, str | None]]:
    return h.screen_buttons()


def _datas(h) -> list[str]:
    return [data or "" for _label, data in _buttons(h)]


async def open_from_manage(h) -> None:
    """Вход в личные кирпичики штатным путём «Управление → Кирпичики»."""
    await h.text("/start", user_id=OWNER_TG)
    await h.callback("manage", user_id=OWNER_TG)
    await h.callback("manage_bricks", user_id=OWNER_TG)


def seed_brick(h) -> int:
    with h.session_factory() as session:
        user = get_or_create_user(session, OWNER_TG)
        brick = create_personal_brick(
            session,
            actor_telegram_user_id=user.telegram_user_id,
            code="назад_1",
            title="Кирпичик возврата",
            value_type="short_text",
        )
        return brick.id


def drop_origin(h) -> None:
    """Старая сессия: payload без источника входа (до 2.5.2)."""
    with h.session_factory() as session:
        row = session.get(BrickSession, OWNER_TG)
        assert row is not None, "сессия кирпичиков не сохранена"
        payload = json.loads(row.payload)
        payload.pop("hub_origin", None)
        row.payload = json.dumps(payload, ensure_ascii=False)
        session.commit()


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class HubBackTests(unittest.TestCase):
    @scenario_test
    async def test_manage_entry_back_returns_to_manage(self, h) -> None:
        await open_from_manage(h)
        self.assertIn(HUB_TITLE, h.screen_text())
        self.assertIn((BACK_LABEL, "bk_back"), _buttons(h))
        state = load_brick_session(OWNER_TG)
        assert state is not None
        self.assertEqual(state.get("hub_origin"), "manage")

        await h.callback("bk_back", user_id=OWNER_TG)
        self.assertIn(MANAGE_TITLE, h.screen_text())
        self.assertIn(("🧩 Кирпичики", "manage_bricks"), _buttons(h))
        self.assertNotIn(HUB_TITLE, h.screen_text())
        self.assertIsNone(
            load_brick_session(OWNER_TG),
            "выход из хаба должен закрывать его сессию",
        )

    @scenario_test
    async def test_back_from_hub_survives_restart(self, h) -> None:
        await open_from_manage(h)
        await h.restart()

        # Старое сообщение-экран и его кнопка живы: сессия читается из БД.
        await h.callback("bk_back", user_id=OWNER_TG)
        self.assertIn(MANAGE_TITLE, h.screen_text())
        self.assertIn(("🧩 Кирпичики", "manage_bricks"), _buttons(h))

    @scenario_test
    async def test_origin_survives_card_and_wizard_round_trip(self, h) -> None:
        brick_id = seed_brick(h)
        await open_from_manage(h)

        # Карточка кирпичика не должна затирать источник входа хаба.
        await h.callback(f"bk_open:{brick_id}", user_id=OWNER_TG)
        self.assertIn("Кирпичик возврата", h.screen_text())
        await h.callback("bk_back", user_id=OWNER_TG)
        self.assertIn(HUB_TITLE, h.screen_text())

        # Мастер создания тоже не теряет источник.
        await h.callback("bk_create", user_id=OWNER_TG)
        self.assertIn("Как назвать кирпичик", h.screen_text())
        await h.callback("bk_back", user_id=OWNER_TG)
        self.assertIn(HUB_TITLE, h.screen_text())

        state = load_brick_session(OWNER_TG)
        assert state is not None
        self.assertEqual(state.get("hub_origin"), "manage")

        await h.callback("bk_back", user_id=OWNER_TG)
        self.assertIn(MANAGE_TITLE, h.screen_text())

    @scenario_test
    async def test_restart_keeps_origin_after_card(self, h) -> None:
        brick_id = seed_brick(h)
        await open_from_manage(h)
        await h.callback(f"bk_open:{brick_id}", user_id=OWNER_TG)
        await h.restart()

        await h.callback("bk_back", user_id=OWNER_TG)
        self.assertIn(HUB_TITLE, h.screen_text())
        await h.callback("bk_back", user_id=OWNER_TG)
        self.assertIn(MANAGE_TITLE, h.screen_text())

    @scenario_test
    async def test_missing_origin_falls_back_to_manage(self, h) -> None:
        # Прямая команда: источник неизвестен, безопасный фолбэк — «Управление».
        await h.text("/bricks", user_id=OWNER_TG)
        self.assertIn(HUB_TITLE, h.screen_text())
        self.assertIn((BACK_LABEL, "bk_back"), _buttons(h))
        await h.callback("bk_back", user_id=OWNER_TG)
        self.assertIn(MANAGE_TITLE, h.screen_text())

    @scenario_test
    async def test_old_session_without_origin_does_not_crash(self, h) -> None:
        await open_from_manage(h)
        drop_origin(h)

        await h.callback("bk_back", user_id=OWNER_TG)
        self.assertIn(MANAGE_TITLE, h.screen_text())
        self.assertFalse(
            [text for text in h.answer_texts() if "внутренняя ошибка" in text.lower()],
            f"показана внутренняя ошибка: {h.answer_texts()}",
        )

    @scenario_test
    async def test_help_round_trip_keeps_hub_origin(self, h) -> None:
        await open_from_manage(h)
        await h.callback("help", user_id=OWNER_TG)
        await h.callback("help_back", user_id=OWNER_TG)

        self.assertIn(HUB_TITLE, h.screen_text())
        state = load_brick_session(OWNER_TG)
        assert state is not None
        self.assertEqual(state.get("hub_origin"), "manage")

        await h.callback("bk_back", user_id=OWNER_TG)
        self.assertIn(MANAGE_TITLE, h.screen_text())

    @scenario_test
    async def test_back_from_hub_opened_in_wizard_returns_to_step(self, h) -> None:
        # Вход в кирпичики с шага конструктора (старый callback `bricks`):
        # «Назад» из хаба возвращает на ТОТ ЖЕ шаг, а не в «Управление».
        from tests.test_stage251_help_return import (
            _choose_personal,
            _seed_personal_template,
            _seed_place,
            _start_to_template,
        )

        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        self.assertIn("Вопрос 1 из 1", h.screen_text())

        await h.callback("bricks")
        self.assertIn(HUB_TITLE, h.screen_text())
        state = load_brick_session(h.user_id)
        assert state is not None
        self.assertEqual(state.get("hub_origin"), "wizard")

        # Источник переживает рестарт: кнопка со старого экрана тоже ведёт на шаг.
        await h.restart()
        await h.callback("bk_back")
        self.assertIn("Вопрос 1 из 1", h.screen_text(), "не вернулись на шаг")
        self.assertNotIn(MANAGE_TITLE, h.screen_text())

    @scenario_test
    async def test_wizard_origin_without_draft_does_not_crash(self, h) -> None:
        from sqlalchemy import delete

        from models import AnnouncementDraft
        from tests.test_stage251_help_return import (
            _choose_personal,
            _seed_personal_template,
            _seed_place,
            _start_to_template,
        )

        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.callback("bricks")

        # Черновика для возврата больше нет: «Назад» не падает и не уводит
        # в «Управление» — конструктор показывает свой безопасный экран.
        with h.session_factory() as session:
            session.execute(delete(AnnouncementDraft))
            session.commit()
        await h.restart()
        await h.callback("bk_back")

        self.assertNotIn(MANAGE_TITLE, h.screen_text())
        self.assertFalse(
            [text for text in h.answer_texts() if "внутренняя ошибка" in text.lower()],
            f"показана внутренняя ошибка: {h.answer_texts()}",
        )

    @scenario_test
    async def test_hub_keeps_main_menu_button(self, h) -> None:
        await open_from_manage(h)
        self.assertIn(("🏠 В меню", "main_menu"), _buttons(h))
        await h.callback("main_menu", user_id=OWNER_TG)
        self.assertIn("Что хотите сделать?", h.screen_text())


if __name__ == "__main__":
    unittest.main()
