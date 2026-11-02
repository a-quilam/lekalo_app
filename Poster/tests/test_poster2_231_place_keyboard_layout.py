# tests/test_poster2_231_place_keyboard_layout.py
"""
Poster 2.3.1 E4 + 2.5 A3: компактная карточка площадки без пустых разделов.

Проверяется раскладка `_place_keyboard` снаружи, как у владельца (§12):

- строка 1: «✏️ Редактировать место» | «📄 Стандартный шаблон»;
- динамическая строка `🧩 Предложения кирпичиков` | `⚔️ Конфликты кодов`
  показывается ТОЛЬКО при наличии актуальных записей; счётчиков `(N)` на
  кнопках нет; оба есть — одна строка, один есть — одна кнопка, ни одного —
  строки нет;
- «🧩 Кирпичики» — отдельная строка;
- администратор: «👤 Назначить администратора» (нет админа, суперадмин) или
  «👤 Управлять администратором» (админ назначен; замена/снятие/передача —
  внутри подраздела);
- «🔗 Ссылка для создания анонса» — отдельная строка перед навигацией;
- навигация «◀️ К списку мест» | «🏠 В меню» — последняя строка;
- строки не шире двух кнопок.

Сценарии идут через настоящий `Application.process_update` и временную
SQLite; прямые доменные вызовы используются только для подготовки данных.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

try:
    from sqlalchemy import select

    from brick_proposal_store import (
        PROPOSAL_STATUS_APPROVED,
        approve_brick_proposal,
        create_brick_proposal,
    )
    from brick_store import create_personal_brick, create_place_brick
    from models import PlaceAdmin, User
    from tests.integration_harness import HARNESS_IMPORT_ERROR, scenario_test
    from tests.test_stage20_admin_flow import ensure_user, insert_place
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)

SUPER_TG = 700100
PLACE_ADMIN_TG = 700200
AUTHOR_TG = 700300
OUTSIDER_TG = 700400


def _rows(h) -> list[list[tuple[str, str | None]]]:
    """Структура клавиатуры: строки и кнопки, как их видит пользователь."""
    record = h.screen()
    assert record is not None, "экрана ещё не было"
    markup = record.get("reply_markup")
    assert markup is not None, "у экрана нет клавиатуры"
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


def _callbacks(h) -> list[str | None]:
    return [data for _label, data in h.screen_buttons()]


def _labels(h) -> list[str]:
    return [label for label, _data in h.screen_buttons()]


def add_place_admin(h, place_id: int, telegram_user_id: int) -> None:
    """Делает пользователя действующим администратором места."""
    session = h.session_factory()
    try:
        user = session.scalar(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        assert user is not None, f"нет пользователя {telegram_user_id}"
        session.add(PlaceAdmin(place_id=place_id, user_id=user.id))
        session.commit()
    finally:
        session.close()


def add_place_proposal(h, place_id: int, *, code: str = "a3_offer") -> int:
    """Pending-предложение кирпичика этой площадке от отдельного автора."""
    session = h.session_factory()
    try:
        get_or_create_user(session, AUTHOR_TG, username="a3-author")
        source = create_personal_brick(
            session,
            actor_telegram_user_id=AUTHOR_TG,
            code=code,
            title="Предложенный A3",
            value_type="short_text",
        )
        session.commit()
        proposal = create_brick_proposal(
            session,
            actor_telegram_user_id=AUTHOR_TG,
            source_brick_id=int(source.id),
            scope="place",
            place_id=place_id,
        )
        session.commit()
        return int(proposal.id)
    finally:
        session.close()


async def add_open_conflict(
    h, place_id: int, *, code: str = "a3_code", manager_tg: int = PLACE_ADMIN_TG
) -> int:
    """Местный носитель кода + одобренное глобальное предложение → задача."""
    session = h.session_factory()
    try:
        get_or_create_user(session, AUTHOR_TG, username="a3-author")
        get_or_create_user(session, manager_tg, username=f"a3-manager-{manager_tg}")
    finally:
        session.close()
    with patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,)):
        session = h.session_factory()
        try:
            create_place_brick(
                session,
                actor_telegram_user_id=manager_tg,
                place_id=place_id,
                code=code,
                title="Местный носитель A3",
                value_type="short_text",
            )
            source = create_personal_brick(
                session,
                actor_telegram_user_id=AUTHOR_TG,
                code=code,
                title="Глобальный носитель A3",
                value_type="short_text",
            )
            session.commit()
            proposal = create_brick_proposal(
                session,
                actor_telegram_user_id=AUTHOR_TG,
                source_brick_id=int(source.id),
                scope="system",
            )
            session.commit()
            proposal_id = int(proposal.id)
            ensure_user(h, SUPER_TG, username="boss", full_name="Супер Админ")
            result = approve_brick_proposal(
                session,
                actor_telegram_user_id=SUPER_TG,
                proposal_id=proposal_id,
            )
            assert result.status == PROPOSAL_STATUS_APPROVED, result.message
            assert result.conflicts, "задача конфликта не создана"
            return int(result.conflicts[0].id)
        finally:
            session.close()


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlaceKeyboardLayoutTests(unittest.TestCase):
    """A3 (§12): раскладка карточки места и динамическая строка."""

    @scenario_test
    async def test_superadmin_without_admin_gets_pairs(self, h) -> None:
        place_id = insert_place(h, "Место без админа")
        ensure_user(h, SUPER_TG, full_name="Супер Админ")
        with patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,)):
            await h.text("/admin", user_id=SUPER_TG)
            await h.callback(f"adm_place:{place_id}", user_id=SUPER_TG)

            self.assertLessEqual(
                max(len(row) for row in _rows(h)), 2, f"строка шире пары: {_rows(h)}"
            )
            rows = _rows(h)
            self.assertEqual(
                [data for _label, data in rows[0]],
                [f"adm_edit_place:{place_id}", f"tp_std:{place_id}"],
            )
            # 0 предложений и 0 конфликтов — динамической строки нет.
            self.assertEqual(
                [data for _label, data in rows[1]], [f"adm_bricks:{place_id}"]
            )
            self.assertNotIn(f"adm_bprop_place:{place_id}", _callbacks(h))
            self.assertNotIn(f"adm_bconflicts:{place_id}", _callbacks(h))
            self.assertEqual(
                [data for _label, data in rows[-1]], ["adm_places", "main_menu"]
            )
            self.assertEqual(
                [data for _label, data in rows[-2]], [f"adm_invite:{place_id}"]
            )
            self.assertEqual(
                [data for _label, data in rows[-3]], [f"adm_assign:{place_id}"]
            )
            self.assertIn(f"adm_assign:{place_id}", _callbacks(h))
            self.assertIn(f"adm_invite:{place_id}", _callbacks(h))

    @scenario_test
    async def test_superadmin_with_admin_gets_manage_subsection_entry(self, h) -> None:
        place_id = insert_place(h, "Место с админом")
        ensure_user(h, SUPER_TG, full_name="Супер Админ")
        ensure_user(h, PLACE_ADMIN_TG, full_name="Местный Админ")
        add_place_admin(h, place_id, PLACE_ADMIN_TG)
        with patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,)):
            await h.text("/admin", user_id=SUPER_TG)
            await h.callback(f"adm_place:{place_id}", user_id=SUPER_TG)

            rows = _rows(h)
            # Замена/снятие больше не вываливаются на карточку: одна точка входа.
            self.assertEqual(
                [data for _label, data in rows[-3]],
                [f"adm_place_admins:{place_id}"],
            )
            self.assertEqual(
                [data for _label, data in rows[-2]], [f"adm_invite:{place_id}"]
            )
            self.assertEqual(
                [data for _label, data in rows[-1]], ["adm_places", "main_menu"]
            )
            self.assertNotIn(f"adm_assign:{place_id}", _callbacks(h))
            self.assertNotIn(f"adm_replace:{place_id}", _callbacks(h))
            self.assertNotIn(f"adm_revoke:{place_id}", _callbacks(h))
            self.assertIn("👤 Управлять администратором", _labels(h))
            self.assertLessEqual(max(len(row) for row in rows), 2)

    @scenario_test
    async def test_place_admin_has_no_superadmin_actions(self, h) -> None:
        place_id = insert_place(h, "Своё место")
        ensure_user(h, PLACE_ADMIN_TG, full_name="Местный Админ")
        add_place_admin(h, place_id, PLACE_ADMIN_TG)
        await h.text("/admin", user_id=PLACE_ADMIN_TG)
        await h.callback(f"adm_place:{place_id}", user_id=PLACE_ADMIN_TG)

        rows = _rows(h)
        self.assertLessEqual(
            max(len(row) for row in rows), 2, f"строка шире пары: {rows}"
        )
        self.assertEqual(
            [data for _label, data in rows[-3]],
            [f"adm_place_admins:{place_id}"],
        )
        self.assertEqual(
            [data for _label, data in rows[-2]], [f"adm_invite:{place_id}"]
        )
        self.assertEqual(
            [data for _label, data in rows[-1]], ["adm_places", "main_menu"]
        )
        callbacks = _callbacks(h)
        for forbidden in ("adm_assign", "adm_replace", "adm_revoke"):
            self.assertFalse(
                [data for data in callbacks if (data or "").startswith(forbidden)],
                f"лишнее действие {forbidden}: {callbacks}",
            )
        self.assertIn(f"adm_invite:{place_id}", callbacks)

    @scenario_test
    async def test_manage_subsection_holds_replace_revoke_back(self, h) -> None:
        place_id = insert_place(h, "Место с админом")
        ensure_user(h, SUPER_TG, full_name="Супер Админ")
        ensure_user(h, PLACE_ADMIN_TG, full_name="Местный Админ")
        add_place_admin(h, place_id, PLACE_ADMIN_TG)
        with patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,)):
            await h.text("/admin", user_id=SUPER_TG)
            await h.callback(f"adm_place:{place_id}", user_id=SUPER_TG)
            await h.callback(f"adm_place_admins:{place_id}", user_id=SUPER_TG)

            text = h.screen_text()
            self.assertIn("Управление администратором", text)
            self.assertIn("Местный Админ", text)
            callbacks = _callbacks(h)
            self.assertIn(f"adm_replace:{place_id}", callbacks)
            self.assertIn(f"adm_revoke:{place_id}", callbacks)
            self.assertIn("adm_place_admins_back", callbacks)
            self.assertEqual(callbacks[-1], "adm_place_admins_back")

            await h.callback("adm_place_admins_back", user_id=SUPER_TG)
            self.assertIn("🛡 Место", h.screen_text())
            self.assertIn(f"adm_place_admins:{place_id}", _callbacks(h))

    @scenario_test
    async def test_manage_subsection_keeps_transfer_for_place_admin(self, h) -> None:
        place_id = insert_place(h, "Своё место")
        ensure_user(h, PLACE_ADMIN_TG, full_name="Местный Админ")
        add_place_admin(h, place_id, PLACE_ADMIN_TG)
        await h.text("/admin", user_id=PLACE_ADMIN_TG)
        await h.callback(f"adm_place:{place_id}", user_id=PLACE_ADMIN_TG)
        await h.callback(f"adm_place_admins:{place_id}", user_id=PLACE_ADMIN_TG)

        labels = _labels(h)
        callbacks = _callbacks(h)
        self.assertIn("📤 Передать админку", labels)
        self.assertIn(f"adm_transfer:{place_id}", callbacks)
        self.assertNotIn(f"adm_replace:{place_id}", callbacks)
        self.assertNotIn(f"adm_revoke:{place_id}", callbacks)

    @scenario_test
    async def test_dynamic_row_one_proposal(self, h) -> None:
        place_id = insert_place(h, "Место с предложением")
        ensure_user(h, PLACE_ADMIN_TG, full_name="Местный Админ")
        add_place_admin(h, place_id, PLACE_ADMIN_TG)
        add_place_proposal(h, place_id)

        await h.text("/admin", user_id=PLACE_ADMIN_TG)
        await h.callback(f"adm_place:{place_id}", user_id=PLACE_ADMIN_TG)

        rows = _rows(h)
        self.assertEqual(
            [data for _label, data in rows[1]], [f"adm_bprop_place:{place_id}"]
        )
        self.assertEqual([data for _label, data in rows[2]], [f"adm_bricks:{place_id}"])
        # Счётчика на кнопке нет.
        self.assertIn("🧩 Предложения кирпичиков", _labels(h))
        self.assertNotIn("🧩 Предложения кирпичиков (1)", _labels(h))

    @scenario_test
    async def test_dynamic_row_one_conflict(self, h) -> None:
        place_id = insert_place(h, "Место с конфликтом")
        ensure_user(h, PLACE_ADMIN_TG, full_name="Местный Админ")
        add_place_admin(h, place_id, PLACE_ADMIN_TG)
        await add_open_conflict(h, place_id)

        await h.text("/admin", user_id=PLACE_ADMIN_TG)
        await h.callback(f"adm_place:{place_id}", user_id=PLACE_ADMIN_TG)

        rows = _rows(h)
        self.assertEqual(
            [data for _label, data in rows[1]], [f"adm_bconflicts:{place_id}"]
        )
        self.assertIn("⚔️ Конфликты кодов", _labels(h))
        self.assertNotIn("⚔️ Конфликты кодов (1)", _labels(h))

    @scenario_test
    async def test_dynamic_row_both_actions(self, h) -> None:
        place_id = insert_place(h, "Место с двумя разделами")
        ensure_user(h, PLACE_ADMIN_TG, full_name="Местный Админ")
        add_place_admin(h, place_id, PLACE_ADMIN_TG)
        add_place_proposal(h, place_id)
        await add_open_conflict(h, place_id)

        await h.text("/admin", user_id=PLACE_ADMIN_TG)
        await h.callback(f"adm_place:{place_id}", user_id=PLACE_ADMIN_TG)

        rows = _rows(h)
        self.assertEqual(
            [data for _label, data in rows[1]],
            [f"adm_bprop_place:{place_id}", f"adm_bconflicts:{place_id}"],
        )
        # Обе кнопки — в ОДНОЙ строке.
        self.assertEqual(len(rows[1]), 2)
        self.assertLessEqual(max(len(row) for row in rows), 2)

    @scenario_test
    async def test_row_disappears_after_last_proposal_approved(self, h) -> None:
        place_id = insert_place(h, "Место ожидания")
        ensure_user(h, PLACE_ADMIN_TG, full_name="Местный Админ")
        add_place_admin(h, place_id, PLACE_ADMIN_TG)
        proposal_id = add_place_proposal(h, place_id, code="a3_last")

        await h.text("/admin", user_id=PLACE_ADMIN_TG)
        await h.callback(f"adm_place:{place_id}", user_id=PLACE_ADMIN_TG)
        self.assertIn(f"adm_bprop_place:{place_id}", _callbacks(h))

        await h.callback(f"adm_bprop_place:{place_id}", user_id=PLACE_ADMIN_TG)
        await h.callback(f"adm_bprop_open:{proposal_id}", user_id=PLACE_ADMIN_TG)
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=PLACE_ADMIN_TG)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=PLACE_ADMIN_TG)
        await h.callback("adm_bprop_back", user_id=PLACE_ADMIN_TG)
        await h.callback("adm_bprop_back", user_id=PLACE_ADMIN_TG)

        # Вернулись на карточку места — счётчик пересчитан при рендере.
        self.assertIn("🛡 Место", h.screen_text())
        self.assertNotIn(f"adm_bprop_place:{place_id}", _callbacks(h))
        rows = _rows(h)
        self.assertEqual([data for _label, data in rows[1]], [f"adm_bricks:{place_id}"])

    @scenario_test
    async def test_outsider_has_no_dynamic_or_admin_buttons(self, h) -> None:
        place_id = insert_place(h, "Чужое место")
        ensure_user(h, SUPER_TG, full_name="Супер Админ")
        ensure_user(h, OUTSIDER_TG, full_name="Посторонний")
        add_place_proposal(h, place_id)
        await add_open_conflict(h, place_id, manager_tg=SUPER_TG)

        # Посторонний в кабинет не попадает: ни карточки, ни действий.
        await h.text("/admin", user_id=OUTSIDER_TG)
        self.assertIn("Раздел доступен только администраторам", h.screen_text())
        await h.callback(f"adm_place_admins:{place_id}", user_id=OUTSIDER_TG)
        self.assertNotIn("Управление администратором", h.screen_text())
