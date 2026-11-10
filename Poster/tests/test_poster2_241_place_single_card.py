# tests/test_poster2_241_place_single_card.py
"""
Poster 2.4.1 T2 — единая карточка площадки (интеграционные сценарии).

До T2 у площадки было два разных экрана: карточка справочника
(«Управление → Площадки → plm_open», с кнопками `plm_edit`/`plm_invite`/
`plm_admins`) и полная административная карточка кабинета (`adm_place`).
Управляющий попадал в справочник, а кнопки вели в кабинет — получалась
двухшаговая навигация и два источника правды.

После T2 карточка одна:

- управляющий местом (и суперадмин) из справочника попадает СРАЗУ в полную
  карточку кабинета: `adm_edit_place`, `tp_std`, `adm_invite`, «◀️ К списку
  мест»;
- промежуточная карточка справочника для управляющего не показывается
  (`plm_edit`/`plm_invite` не существуют — старые кнопки получают
  stale-ответ);
- не-управляющий видит только просмотр справочника («🛡 Я админ места»);
- экран ссылки на создание анонса — часть полной карточки (`adm_invite`),
  формат ссылки не изменился;
- «◀️ К списку мест» возвращает туда, откуда карточку открыли: в справочник
  (с сохранением страницы) или в список кабинета.

Все сценарии идут через настоящее приложение (`bot.build_application`),
FakeBot и временную SQLite (`tests.integration_harness`): рабочая
`post_bot.db`, `.env` и сеть Telegram не используются.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import select

from tests import ui_texts_expect as cat

try:
    from admin_state import load_admin_session
    from models import PlaceAdmin, Template, utcnow
    from place_state import load_place_session
    from place_store import create_place
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(exc)

OTHER_TG = 8_971_002
SUPERADMIN_TG = 8_971_003

# Текст просмотровой карточки справочника: её больше не должно быть на пути
# управляющего.
HANDBOOK_VIEW_MARKER = "можно посмотреть"
STALE_MARKER = "больше не работает"


# --- Помощники ---------------------------------------------------------------


def _seed_place(
    h,
    *,
    name: str = "Дом культуры",
    actor_tg: int = DEFAULT_USER_ID,
    admin: bool = False,
    address: str | None = "ул. Тестовая, 1",
    url: str | None = "https://example.com/place",
) -> int:
    """Место со стандартным шаблоном; `admin=True` выдаёт роль админа места."""
    with h.session_factory() as session:
        user = get_or_create_user(session, actor_tg, username=f"user-{actor_tg}")
        place = create_place(
            session,
            name,
            city="Тестовый город",
            created_by_user_id=user.id,
            address=address,
            url=url,
        )
        if admin:
            session.add(
                PlaceAdmin(
                    place_id=place.id,
                    user_id=user.id,
                    assigned_by_user_id=None,
                    assigned_at=utcnow(),
                )
            )
        session.commit()
        return int(place.id)


def _callbacks(h, prefix: str) -> list[str]:
    return [
        data
        for _label, data in h.screen_buttons()
        if data is not None and data.startswith(prefix)
    ]


def _callback(h, prefix: str) -> str:
    found = _callbacks(h, prefix)
    if not found:
        raise AssertionError(f"Нет callback {prefix!r}; есть: {h.screen_buttons()}")
    return found[0]


def _last_alert(h) -> str:
    answers = h.answers()
    if not answers:
        return ""
    record = answers[-1]
    args = record.get("args") or ()
    return str(args[0]) if args else str(record.get("text") or "")


def _token_from_screen(h) -> str:
    text = h.screen_text()
    marker = "?start="
    if marker not in text:
        raise AssertionError(f"В экране нет ссылки: {text!r}")
    return text.split(marker, 1)[1].split()[0].strip()


def _active_invites_count(h) -> int:
    from models import PlaceInviteLink

    with h.session_factory() as session:
        rows = session.scalars(select(PlaceInviteLink)).all()
        return len([row for row in rows if row.revoked_at is None])


async def _open_handbook(h, *, user_id: int = DEFAULT_USER_ID) -> None:
    await h.text("/start", user_id=user_id)
    await h.callback("manage_places", user_id=user_id)


# --- Единая карточка: кто и что видит ---------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class SinglePlaceCardTests(unittest.TestCase):
    """Управляющий сразу получает полную карточку, чужой — только просмотр."""

    @scenario_test
    async def test_place_admin_lands_on_full_card_without_intermediate(self, h):
        place_id = _seed_place(h, name="Дом культуры", admin=True)

        await _open_handbook(h)
        await h.callback(f"plm_open:{place_id}")

        callbacks = [data for _label, data in h.screen_buttons()]
        self.assertIn(f"adm_edit_place:{place_id}", callbacks)
        self.assertIn(f"tp_std:{place_id}", callbacks)
        self.assertIn(f"adm_invite:{place_id}", callbacks)
        self.assertIn("adm_places", callbacks)
        # Старой карточки справочника и её кнопок больше нет.
        self.assertNotIn(f"plm_edit:{place_id}", callbacks)
        self.assertNotIn(f"plm_invite:{place_id}", callbacks)
        self.assertNotIn(f"plm_admins:{place_id}", callbacks)
        self.assertNotIn(f"plm_admin_request:{place_id}", callbacks)

        # Это именно карточка кабинета, а не просмотр справочника.
        self.assertIn("🛡 Место", h.screen_text())
        self.assertIn("Дом культуры", h.screen_text())
        for text in h.screen_texts():
            cat.assert_not_shows(
                self,
                text,
                "places.manage_card.eto_prosmotr_spravochnika",
                "промежуточная карточка справочника не должна показываться",
            )

        admin_state = load_admin_session(DEFAULT_USER_ID)
        self.assertIsNotNone(admin_state)
        assert admin_state is not None
        self.assertEqual(admin_state["phase"], "place")
        self.assertEqual(admin_state["place_id"], place_id)
        # Карточка открыта из справочника — возврат пойдёт туда же.
        self.assertEqual(admin_state.get("return_to"), "manage")

    @scenario_test
    async def test_superadmin_sees_same_full_card_with_admin_actions(self, h):
        place_id = _seed_place(h, name="Любое место", actor_tg=OTHER_TG)
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,)):
            await _open_handbook(h, user_id=SUPERADMIN_TG)
            await h.callback(f"plm_open:{place_id}", user_id=SUPERADMIN_TG)

            callbacks = [data for _label, data in h.screen_buttons()]
            self.assertIn(f"adm_edit_place:{place_id}", callbacks)
            self.assertIn(f"tp_std:{place_id}", callbacks)
            self.assertIn(f"adm_invite:{place_id}", callbacks)
            # Суперадминские действия места остаются на месте.
            self.assertIn(f"adm_assign:{place_id}", callbacks)
            self.assertIn("adm_places", callbacks)
            self.assertNotIn(f"plm_edit:{place_id}", callbacks)
            self.assertNotIn(f"plm_invite:{place_id}", callbacks)

            admin_state = load_admin_session(SUPERADMIN_TG)
            self.assertIsNotNone(admin_state)
            assert admin_state is not None
            self.assertEqual(admin_state["phase"], "place")
            self.assertEqual(admin_state["place_id"], place_id)

    @scenario_test
    async def test_non_manager_keeps_view_card_without_admin_actions(self, h):
        place_id = _seed_place(h, name="Чужое место", actor_tg=OTHER_TG)

        await _open_handbook(h)
        await h.callback(f"plm_open:{place_id}")

        callbacks = [data for _label, data in h.screen_buttons()]
        self.assertIn(f"plm_admin_request:{place_id}", callbacks)
        self.assertIn("plm_back", callbacks)
        self.assertIn("main_menu", callbacks)
        self.assertNotIn(f"plm_edit:{place_id}", callbacks)
        self.assertNotIn(f"plm_invite:{place_id}", callbacks)
        self.assertNotIn(f"adm_edit_place:{place_id}", callbacks)
        self.assertNotIn(f"adm_invite:{place_id}", callbacks)
        cat.assert_shows(
            self, h.screen_text(), "places.manage_card.eto_prosmotr_spravochnika"
        )

        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "manage_card")
        self.assertIsNone(load_admin_session(DEFAULT_USER_ID))


# --- Экран ссылки: формат не изменился --------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class InviteScreenFromFullCardTests(unittest.TestCase):
    """«🔗 Ссылка для создания анонса» живёт в полной карточке (T2)."""

    @scenario_test
    async def test_invite_screen_keeps_link_format_and_returns_to_card(self, h):
        place_id = _seed_place(h, name="Дом культуры", admin=True)

        await _open_handbook(h)
        await h.callback(f"plm_open:{place_id}")
        await h.callback(f"adm_invite:{place_id}")

        text = h.screen_text()
        token = _token_from_screen(h)
        # Формат ссылки прежний (Poster 2.2 U4b): username бота + `?start=<token>`.
        self.assertIn(f"https://t.me/{h.bot.username}?start={token}", text)
        self.assertIn("Ссылка для создания анонса", text)
        self.assertGreaterEqual(len(token), 16)
        self.assertLessEqual(len(token), 22)
        self.assertTrue(
            all(char.isalnum() or char in {"_", "-"} for char in token), token
        )
        self.assertNotEqual(token, str(place_id))
        # Превью ссылки отключено самим экраном.
        record = h.screen()
        assert record is not None
        preview = record.get("link_preview_options")
        self.assertIsNotNone(preview)
        self.assertTrue(getattr(preview, "is_disabled", False))

        admin_state = load_admin_session(DEFAULT_USER_ID)
        self.assertIsNotNone(admin_state)
        assert admin_state is not None
        self.assertEqual(admin_state["phase"], "invite")
        self.assertEqual(admin_state["place_id"], place_id)

        # «◀️ К карточке» возвращает в полную карточку того же места.
        await h.callback("adm_invite_back")
        callbacks = [data for _label, data in h.screen_buttons()]
        self.assertIn(f"adm_edit_place:{place_id}", callbacks)
        self.assertIn(f"adm_invite:{place_id}", callbacks)
        admin_state = load_admin_session(DEFAULT_USER_ID)
        assert admin_state is not None
        self.assertEqual(admin_state["phase"], "place")
        self.assertEqual(admin_state["place_id"], place_id)

    @scenario_test
    async def test_reopen_does_not_duplicate_and_no_regenerate(self, h):
        place_id = _seed_place(h, admin=True)

        await _open_handbook(h)
        await h.callback(f"plm_open:{place_id}")
        await h.callback(f"adm_invite:{place_id}")
        first = _token_from_screen(h)
        self.assertEqual(_active_invites_count(h), 1)

        # Повторный вход не создаёт дублей: та же ссылка.
        await h.callback("adm_invite_back")
        await h.callback(f"adm_invite:{place_id}")
        self.assertEqual(_token_from_screen(h), first)
        self.assertEqual(_active_invites_count(h), 1)

        # Poster 2.6.1 §8.2: пользовательского перевыпуска больше нет.
        self.assertEqual(_callbacks(h, "adm_invite_regen"), [])
        before = h.screen_text()
        await h.callback(f"adm_invite_regen:{place_id}")
        cat.assert_stale(self, _last_alert(h))
        self.assertEqual(h.screen_text(), before)
        self.assertEqual(_token_from_screen(h), first)
        self.assertEqual(_active_invites_count(h), 1)


# --- «К списку мест»: возврат туда, откуда пришли ---------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlacesBackDestinationTests(unittest.TestCase):
    """`adm_places` возвращает в справочник или в список кабинета."""

    @scenario_test
    async def test_back_from_handbook_card_returns_to_handbook(self, h):
        # Девять мест: вторая страница справочника (5 записей на страницу).
        ids = [_seed_place(h, name=f"Место {index}", admin=True) for index in range(9)]
        target = ids[-1]

        await _open_handbook(h)
        await h.callback("plm_page:5")
        self.assertIn(
            ("2/2", "noop:2:2"), h.screen_buttons(), "индикатор второй страницы"
        )
        await h.callback(f"plm_open:{target}")
        self.assertIn("🛡 Место", h.screen_text())

        await h.callback("adm_places")
        # Вернулись в справочник, а не в список кабинета.
        self.assertIn("📍 Площадки", h.screen_text())
        self.assertIn(
            ("2/2", "noop:2:2"),
            h.screen_buttons(),
            "страница справочника сохранена",
        )
        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "manage_list")
        self.assertEqual(state.get("page"), 5)
        self.assertIsNone(load_admin_session(DEFAULT_USER_ID))

    @scenario_test
    async def test_back_from_admin_panel_card_returns_to_cabinet_list(self, h):
        place_id = _seed_place(h, name="Дом культуры", admin=True)

        await h.text("/start")
        await h.callback("manage")
        await h.callback("admin_panel")
        self.assertIn(f"adm_place:{place_id}", _callbacks(h, "adm_place:"))
        await h.callback(f"adm_place:{place_id}")
        self.assertIn("🛡 Место", h.screen_text())

        await h.callback("adm_places")
        state = load_admin_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "places")
        self.assertNotIn("return_to", state)
        # Список кабинета, а не справочник «Управление → Площадки».
        self.assertIn("места", h.screen_text().lower())
        self.assertNotIn("📍 Площадки", h.screen_text())


# --- Возвраты в полную карточку --------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FullCardReturnTests(unittest.TestCase):
    """«Отмена» и «К месту» ведут в полную карточку, а не в справочник."""

    @scenario_test
    async def test_cancel_from_edit_menu_returns_to_full_card(self, h):
        place_id = _seed_place(h, name="Дом культуры", admin=True)

        await _open_handbook(h)
        await h.callback(f"plm_open:{place_id}")
        await h.callback(f"adm_edit_place:{place_id}")
        state = load_admin_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertEqual(state["phase"], "edit_menu")

        await h.callback("adm_cancel")
        state = load_admin_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertEqual(state["phase"], "place")
        self.assertEqual(state["place_id"], place_id)
        callbacks = [data for _label, data in h.screen_buttons()]
        self.assertIn(f"adm_edit_place:{place_id}", callbacks)
        self.assertIn(f"adm_invite:{place_id}", callbacks)
        # Карточка осталась «справочной»: возврат к списку — в справочник.
        self.assertEqual(state.get("return_to"), "manage")

    @scenario_test
    async def test_standard_template_back_returns_to_full_card(self, h):
        place_id = _seed_place(h, name="Дом культуры", admin=True)

        await _open_handbook(h)
        await h.callback(f"plm_open:{place_id}")
        await h.callback(f"tp_std:{place_id}")
        # 2.6.1 §6: шапка карточки — «📄 Шаблон — {площадка}».
        cat.assert_shows(self, h.screen_text(), "templates.card.title")

        await h.callback(_callback(h, "tp_back_place:"))
        state = load_admin_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "place")
        self.assertEqual(state["place_id"], place_id)
        self.assertIn("🛡 Место", h.screen_text())


# --- Устаревшие кнопки и подделки ------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class StaleAndForgedButtonsTests(unittest.TestCase):
    """Старые кнопки молча не работают; подделка прав не расширяет."""

    @scenario_test
    async def test_old_plm_buttons_are_stale_and_change_nothing(self, h):
        own = _seed_place(h, name="Своё", admin=True)
        foreign = _seed_place(h, name="Чужое", actor_tg=OTHER_TG)

        await _open_handbook(h)
        await h.callback(f"plm_open:{own}")
        admin_before = load_admin_session(DEFAULT_USER_ID)
        assert admin_before is not None

        with_invites_before = _active_invites_count(h)

        # Кнопки из старых сообщений: `plm_edit`/`plm_invite` больше не существуют.
        await h.callback(f"plm_edit:{foreign}")
        cat.assert_stale(self, _last_alert(h))
        await h.callback(f"plm_invite:{own}")
        cat.assert_stale(self, _last_alert(h))
        await h.callback(f"plm_card:{own}")
        cat.assert_stale(self, _last_alert(h))

        # Экран и состояние не переключились, ссылки не появились.
        admin_after = load_admin_session(DEFAULT_USER_ID)
        assert admin_after is not None
        self.assertEqual(admin_after["phase"], "place")
        self.assertEqual(admin_after["place_id"], own)
        self.assertEqual(_active_invites_count(h), with_invites_before)
        self.assertNotIn("?start=", h.screen_text())

    @scenario_test
    async def test_forged_adm_invite_for_foreign_place_is_refused(self, h):
        foreign = _seed_place(h, name="Чужое место", actor_tg=OTHER_TG)

        await _open_handbook(h)
        await h.callback(f"plm_open:{foreign}")
        # Просмотр чужого места: кнопки ссылки нет вовсе.
        self.assertEqual(_callbacks(h, "adm_invite:"), [])

        await h.callback(f"adm_invite:{foreign}")
        alert = _last_alert(h).lower()
        self.assertTrue(alert, "подделанная кнопка должна получить отказ")
        cat.assert_stale(self, alert)
        self.assertEqual(_active_invites_count(h), 0, "ссылка не выдана")
        self.assertNotIn("?start=", h.screen_text())
        state = load_place_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertEqual(state["phase"], "manage_card")
        self.assertIsNone(load_admin_session(DEFAULT_USER_ID))

    @scenario_test
    async def test_open_card_of_own_place_cannot_invite_foreign(self, h):
        """
        Управляющий своим местом с открытой полной карточкой не получает
        ссылку чужого места подделанным `adm_invite:<чужой id>`.
        """
        own = _seed_place(h, name="Своё место", admin=True)
        foreign = _seed_place(h, name="Чужое место", actor_tg=OTHER_TG)

        await _open_handbook(h)
        await h.callback(f"plm_open:{own}")
        admin_state = load_admin_session(DEFAULT_USER_ID)
        assert admin_state is not None
        self.assertEqual(admin_state["phase"], "place")

        await h.callback(f"adm_invite:{foreign}")
        cat.assert_stale(self, _last_alert(h))
        self.assertEqual(_active_invites_count(h), 0, "ссылка не выдана")
        admin_state = load_admin_session(DEFAULT_USER_ID)
        assert admin_state is not None
        self.assertEqual(admin_state["phase"], "place")
        self.assertEqual(admin_state["place_id"], own, "карточка своего места цела")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class SingleCardDataIntegrityTests(unittest.TestCase):
    """Единая карточка показывает данные места и не плодит шаблонов."""

    @scenario_test
    async def test_full_card_shows_place_data_and_keeps_one_standard(self, h):
        place_id = _seed_place(
            h,
            name="Дом культуры",
            admin=True,
            address="ул. Ленина, 1",
            url="https://example.com/place",
        )

        await _open_handbook(h)
        await h.callback(f"plm_open:{place_id}")

        text = h.screen_text()
        self.assertIn("Дом культуры", text)
        self.assertIn("ул. Ленина, 1", text)
        self.assertIn("https://example.com/place", text)

        with h.session_factory() as session:
            templates = session.scalars(
                select(Template).where(
                    Template.place_id == place_id, Template.kind == "standard"
                )
            ).all()
        self.assertEqual(len(templates), 1, "переходы не создают второй стандарт")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
