# tests/test_stage25_acceptance_a3a4.py
"""
Poster 2.5, независимая приёмка A3+A4 (Tester).

Проверяется утверждённое пользовательское поведение (spec §12, §13,
§57 пп. 6–12), а не реализация Developer:

A3 — единая административная карточка места: раскладка и callback-данные;
динамическая строка «предложения / конфликты» показывается только при
актуальных записях, без пустых разделов и счётчиков `(0)`; после обработки
последней записи кнопка исчезает, при новой — появляется; администратор
места: «Назначить» / «Управлять администратором» → подраздел (замена,
снятие, передача админки, возврат к карточке); посторонний не видит
админ-действий, подделанный callback отклоняется сервером.

A4 — локальные кирпичики площадки: «➕ Создать кирпичик» в разделе кирпичиков
площадки открывает существующий конструктор; сохранённый кирпичик сразу
принадлежит площадке (`scope=place`), не личный и не предложение, без
согласования; возврат — в список кирпичиков той же площадки; «◀️ Назад» из
раздела кирпичиков ведёт в административную карточку этой площадки (в том
числе после рестарта, без stale-alert); навигация не размножает служебные
сообщения.

Сценарии идут через настоящее `Application.process_update`, FakeBot и
временную SQLite (`tests.integration_harness`). Реальная база, сеть Telegram
и настоящий бот не используются.

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage25_acceptance_a3a4 -v
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import func, select

from admin_state import load_admin_session, save_admin_session
from brick_proposal_store import (
    PROPOSAL_STATUS_APPROVED,
    approve_brick_proposal,
    create_brick_proposal,
)
from brick_state import load_brick_session
from brick_store import create_personal_brick, create_place_brick
from models import Brick, BrickConflict, BrickProposal, PlaceAdmin, User, utcnow
from place_store import create_place
from tests import ui_texts_expect as cat
from tests.integration_harness import (
    HARNESS_IMPORT_ERROR,
    Harness,
    scenario_test,
)
from user_store import get_or_create_user

SUPER_TG = 2_505_001
ADMIN_TG = 2_505_002
OTHER_ADMIN_TG = 2_505_003
AUTHOR_TG = 2_505_004
STRANGER_TG = 2_505_005

DENIED_ALERT = "У вас нет доступа к разделу администрирования."
STALE_MARK = "эта кнопка больше не работает"

CREATE_LABEL = "➕ Создать кирпичик"
CARD_TITLE = "🛡 Место"
PROPOSALS_LABEL = "🧩 Предложения кирпичиков"
CONFLICTS_LABEL = "⚔️ Конфликты кодов"
ASSIGN_LABEL = "👤 Назначить администратора"
MANAGE_LABEL = "👤 Управлять администратором"


# ---------------------------------------------------------------------------
# помощники
# ---------------------------------------------------------------------------


def ensure_user(h: Harness, tg: int, *, full_name: str | None = None) -> int:
    with h.session_factory() as session:
        user = get_or_create_user(
            session, tg, username=f"u{tg}", full_name=full_name or f"Пользователь {tg}"
        )
        return int(user.id)


def make_place(h: Harness, name: str, *, admin_tg: int | None = None) -> int:
    with h.session_factory() as session:
        creator = None
        if admin_tg is not None:
            creator = get_or_create_user(
                session, admin_tg, username=f"a{admin_tg}", full_name="Админ места"
            )
        place = create_place(
            session,
            name,
            city="Город приёмки",
            created_by_user_id=creator.id if creator is not None else None,
        )
        if creator is not None:
            session.add(
                PlaceAdmin(place_id=place.id, user_id=creator.id, assigned_at=utcnow())
            )
            session.commit()
        return int(place.id)


def add_pending_proposal(h: Harness, place_id: int, *, code: str) -> int:
    ensure_user(h, AUTHOR_TG, full_name="Автор предложения")
    with h.session_factory() as session:
        source = create_personal_brick(
            session,
            actor_telegram_user_id=AUTHOR_TG,
            code=code,
            title=f"Предложение {code}",
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


def add_open_conflict(
    h: Harness, place_id: int, *, code: str, manager_tg: int = ADMIN_TG
) -> int:
    """Местный носитель кода + одобренный глобальный кирпичик → открытая задача."""
    ensure_user(h, AUTHOR_TG, full_name="Автор предложения")
    ensure_user(h, manager_tg, full_name="Админ места")
    ensure_user(h, SUPER_TG, full_name="Супер Админ")
    with h.session_factory() as session:
        create_place_brick(
            session,
            actor_telegram_user_id=manager_tg,
            place_id=place_id,
            code=code,
            title=f"Местный носитель {code}",
            value_type="short_text",
        )
        source = create_personal_brick(
            session,
            actor_telegram_user_id=AUTHOR_TG,
            code=code,
            title=f"Глобальный носитель {code}",
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
        result = approve_brick_proposal(
            session,
            actor_telegram_user_id=SUPER_TG,
            proposal_id=proposal_id,
        )
        assert result.status == PROPOSAL_STATUS_APPROVED, result.message
        assert result.conflicts, "конфликт кодов не создан"
        return int(result.conflicts[0].id)


def rows(h: Harness) -> list[list[tuple[str, str | None]]]:
    record = h.screen()
    assert record is not None, "экрана ещё не было"
    markup = record.get("reply_markup")
    assert markup is not None, "у экрана нет клавиатуры"
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


def labels(h: Harness) -> list[str]:
    return [label for label, _data in h.screen_buttons()]


def callbacks(h: Harness) -> list[str | None]:
    return [data for _label, data in h.screen_buttons()]


def alert(h: Harness) -> str:
    """Текст последнего ответа на callback (alert)."""
    if not h.answers():
        return ""
    record = h.answers()[-1]
    args = tuple(record.get("args") or ())
    if args and isinstance(args[0], str):
        return args[0]
    return str(record.get("text") or "")


def screen_id(h: Harness, tg: int) -> int | None:
    """message_id текущего служебного экрана пользователя (по user_data/БД)."""
    ref = h.user_data(user_id=tg).get("screen")
    return int(ref["message_id"]) if isinstance(ref, dict) else None


def screen_message_ids(h: Harness) -> list[int]:
    """message_id каждого СОЗДАННОГО служебного экрана (не правки)."""
    found: list[int] = []
    deleted = set(h.deleted_ids())
    for record in h.screens():
        if record["type"] != "message":
            continue
        ids = h.bot.call_message_ids.get(id(record), ())
        if not ids:
            continue
        message_id = int(ids[0])
        if message_id in deleted:
            continue
        found.append(message_id)
    return found


def edited_message_ids(h: Harness) -> list[int]:
    out: list[int] = []
    for record in h.edits():
        message_id = record.get("message_id")
        if message_id is None:
            args = tuple(record.get("args") or ())
            message_id = args[1] if len(args) > 1 else None
        if message_id is not None:
            out.append(int(message_id))
    return out


def place_bricks(h: Harness, place_id: int) -> list[Brick]:
    with h.session_factory() as session:
        return list(
            session.scalars(
                select(Brick)
                .where(Brick.scope == "place", Brick.place_id == place_id)
                .order_by(Brick.id)
            ).all()
        )


def proposal_count(h: Harness) -> int:
    with h.session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(BrickProposal)) or 0)


async def open_place_card(h: Harness, place_id: int, *, tg: int = ADMIN_TG) -> None:
    await h.text("/admin", user_id=tg)
    await h.callback(f"adm_place:{place_id}", user_id=tg)


async def open_place_bricks(h: Harness, place_id: int, *, tg: int = ADMIN_TG) -> None:
    await h.callback(f"adm_bricks:{place_id}", user_id=tg)


async def fill_local_brick(
    h: Harness, *, title: str, code: str, tg: int = ADMIN_TG
) -> None:
    await h.text(title, user_id=tg)
    await h.text(code, user_id=tg)
    await h.callback("bk_skip_description", user_id=tg)
    await h.callback("bk_type:short_text", user_id=tg)
    await h.callback("bk_save", user_id=tg)


def _skip_if_unavailable(cls):
    return unittest.skipIf(
        HARNESS_IMPORT_ERROR is not None,
        f"интеграционное окружение недоступно: {HARNESS_IMPORT_ERROR}",
    )(cls)


# ---------------------------------------------------------------------------
# A3 (§12): карточка места
# ---------------------------------------------------------------------------


@_skip_if_unavailable
class PlaceCardLayoutAcceptance(unittest.TestCase):
    """П. 1–3, 5–6: раскладка, динамическая строка, админ-действия, права."""

    def setUp(self) -> None:
        self._patchers = [patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,))]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    @scenario_test
    async def test_01_single_card_rows_and_callbacks(self, h: Harness) -> None:
        place_id = make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await open_place_card(h, place_id)
        self.assertIn(CARD_TITLE, h.screen_text())
        grid = rows(h)
        self.assertEqual(
            [data for _label, data in grid[0]],
            [f"adm_edit_place:{place_id}", f"tp_std:{place_id}"],
            f"первая строка карточки не та: {grid}",
        )
        self.assertEqual(
            [label for label, _data in grid[0]],
            ["✏️ Редактировать место", "📄 Шаблон"],
        )
        # 0 предложений и 0 конфликтов → динамической строки нет.
        self.assertEqual(
            [data for _label, data in grid[1]],
            [f"adm_bricks:{place_id}"],
            f"нет строки «Кирпичики» сразу после первой: {grid}",
        )
        self.assertEqual([label for label, _d in grid[1]], ["🧩 Кирпичики"])
        # Управляющий местом (он же админ) видит «Управлять администратором».
        self.assertEqual(
            [data for _label, data in grid[-3]], [f"adm_place_admins:{place_id}"]
        )
        self.assertEqual([label for label, _d in grid[-3]], [MANAGE_LABEL])
        self.assertEqual(
            [data for _label, data in grid[-2]], [f"adm_invite:{place_id}"]
        )
        self.assertEqual(
            [label for label, _d in grid[-2]], ["🔗 Ссылка для создания анонса"]
        )
        self.assertEqual(
            [data for _label, data in grid[-1]], ["adm_places", "main_menu"]
        )
        self.assertEqual(
            [label for label, _d in grid[-1]], ["◀️ К списку мест", "🏠 В меню"]
        )
        self.assertLessEqual(max(len(row) for row in grid), 2, grid)

    @scenario_test
    async def test_02_no_empty_sections_and_no_counters(self, h: Harness) -> None:
        place_id = make_place(h, "Пустое место", admin_tg=ADMIN_TG)
        await open_place_card(h, place_id)
        data = callbacks(h)
        self.assertNotIn(f"adm_bprop_place:{place_id}", data)
        self.assertNotIn(f"adm_bconflicts:{place_id}", data)
        text = " ".join(labels(h))
        self.assertNotIn("(0)", text, f"пустой раздел со счётчиком: {text}")
        self.assertNotIn("Предложения кирпичиков", text)
        self.assertNotIn("Конфликты кодов", text)

    @scenario_test
    async def test_03_dynamic_row_variants(self, h: Harness) -> None:
        # только предложение
        proposal_place = make_place(h, "Место с предложением", admin_tg=ADMIN_TG)
        add_pending_proposal(h, proposal_place, code="предложение_1")
        await open_place_card(h, proposal_place)
        grid = rows(h)
        self.assertEqual(
            [data for _label, data in grid[1]], [f"adm_bprop_place:{proposal_place}"]
        )
        self.assertEqual([label for label, _d in grid[1]], [PROPOSALS_LABEL])
        self.assertNotIn(f"adm_bconflicts:{proposal_place}", callbacks(h))
        joined = " ".join(labels(h))
        self.assertNotIn("(1)", joined, f"счётчик на кнопке: {joined}")
        self.assertNotIn("(0)", joined, f"счётчик на кнопке: {joined}")

        # только конфликт
        await h.callback("main_menu", user_id=ADMIN_TG)
        conflict_place = make_place(h, "Место с конфликтом", admin_tg=ADMIN_TG)
        add_open_conflict(h, conflict_place, code="конфликт_1")
        await open_place_card(h, conflict_place)
        grid = rows(h)
        self.assertEqual(
            [data for _label, data in grid[1]], [f"adm_bconflicts:{conflict_place}"]
        )
        self.assertEqual([label for label, _d in grid[1]], [CONFLICTS_LABEL])
        self.assertNotIn(f"adm_bprop_place:{conflict_place}", callbacks(h))

        # оба — одна строка
        await h.callback("main_menu", user_id=ADMIN_TG)
        both_place = make_place(h, "Место с двумя разделами", admin_tg=ADMIN_TG)
        add_pending_proposal(h, both_place, code="оба_1")
        add_open_conflict(h, both_place, code="оба_2")
        await open_place_card(h, both_place)
        grid = rows(h)
        self.assertEqual(
            [data for _label, data in grid[1]],
            [f"adm_bprop_place:{both_place}", f"adm_bconflicts:{both_place}"],
            f"обе кнопки не в одной строке: {grid}",
        )
        self.assertEqual(len(grid[1]), 2)
        self.assertEqual(grid[2], [("🧩 Кирпичики", f"adm_bricks:{both_place}")])
        self.assertLessEqual(max(len(row) for row in grid), 2)

    @scenario_test
    async def test_04_proposal_button_disappears_and_returns(self, h: Harness) -> None:
        place_id = make_place(h, "Очередь места", admin_tg=ADMIN_TG)
        proposal_id = add_pending_proposal(h, place_id, code="последнее")
        await open_place_card(h, place_id)
        self.assertIn(f"adm_bprop_place:{place_id}", callbacks(h))

        # Обработка последнего предложения через интерфейс.
        await h.callback(f"adm_bprop_place:{place_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bprop_open:{proposal_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=ADMIN_TG)
        await h.callback("adm_bprop_back", user_id=ADMIN_TG)
        await h.callback("adm_bprop_back", user_id=ADMIN_TG)

        self.assertIn(CARD_TITLE, h.screen_text())
        self.assertNotIn(
            f"adm_bprop_place:{place_id}",
            callbacks(h),
            "после одобрения последнего предложения кнопка осталась",
        )
        self.assertEqual([data for _l, data in rows(h)[1]], [f"adm_bricks:{place_id}"])

        # Новая запись → кнопка появляется при следующем отображении.
        add_pending_proposal(h, place_id, code="новое")
        await h.callback(f"adm_bricks:{place_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_place:{place_id}", user_id=ADMIN_TG)
        self.assertIn(
            f"adm_bprop_place:{place_id}",
            callbacks(h),
            "новая запись не вернула кнопку предложений",
        )

    @scenario_test
    async def test_04b_reject_last_proposal_hides_button(self, h: Harness) -> None:
        place_id = make_place(h, "Место отказа", admin_tg=ADMIN_TG)
        proposal_id = add_pending_proposal(h, place_id, code="отклоняемое")
        await open_place_card(h, place_id)
        await h.callback(f"adm_bprop_place:{place_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bprop_open:{proposal_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bprop_reject:{proposal_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bprop_reject_yes:{proposal_id}", user_id=ADMIN_TG)
        await h.callback("adm_bprop_back", user_id=ADMIN_TG)
        await h.callback("adm_bprop_back", user_id=ADMIN_TG)
        self.assertIn(CARD_TITLE, h.screen_text())
        self.assertNotIn(
            f"adm_bprop_place:{place_id}",
            callbacks(h),
            "после отклонения последнего предложения кнопка осталась",
        )

    @scenario_test
    async def test_04c_conflict_button_disappears_after_resolution(
        self, h: Harness
    ) -> None:
        place_id = make_place(h, "Место конфликта", admin_tg=ADMIN_TG)
        conflict_id = add_open_conflict(h, place_id, code="разрешаемый")
        await open_place_card(h, place_id)
        self.assertIn(f"adm_bconflicts:{place_id}", callbacks(h))

        await h.callback(f"adm_bconflicts:{place_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bconflict:{conflict_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bconflict_switch:{conflict_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bconflict_switch_yes:{conflict_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bconflicts:{place_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_place:{place_id}", user_id=ADMIN_TG)

        self.assertIn(CARD_TITLE, h.screen_text())
        self.assertNotIn(
            f"adm_bconflicts:{place_id}",
            callbacks(h),
            "после решения последнего конфликта кнопка осталась",
        )
        with h.session_factory() as session:
            conflict = session.get(BrickConflict, conflict_id)
            assert conflict is not None
            self.assertNotEqual(conflict.status, "open")

        # Новая задача конфликта → кнопка появляется при следующем показе.
        add_open_conflict(h, place_id, code="новый_конфликт")
        await h.callback(f"adm_bricks:{place_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_place:{place_id}", user_id=ADMIN_TG)
        self.assertIn(f"adm_bconflicts:{place_id}", callbacks(h))

    @scenario_test
    async def test_05_admin_assignment_entry_and_subsection(self, h: Harness) -> None:
        place_id = make_place(h, "Место без админа")
        ensure_user(h, SUPER_TG, full_name="Супер Админ")
        await open_place_card(h, place_id, tg=SUPER_TG)
        self.assertIn(ASSIGN_LABEL, labels(h))
        self.assertNotIn(MANAGE_LABEL, labels(h))
        self.assertIn(f"adm_assign:{place_id}", callbacks(h))

        # Назначаем администратора и смотрим подраздел суперадмином.
        ensure_user(h, ADMIN_TG, full_name="Назначаемый Админ")
        with h.session_factory() as session:
            user = session.scalar(select(User).where(User.telegram_user_id == ADMIN_TG))
            assert user is not None
            session.add(
                PlaceAdmin(place_id=place_id, user_id=user.id, assigned_at=utcnow())
            )
            session.commit()
        await h.callback(f"adm_bricks:{place_id}", user_id=SUPER_TG)
        await h.callback(f"adm_place:{place_id}", user_id=SUPER_TG)
        self.assertIn(MANAGE_LABEL, labels(h))
        self.assertNotIn(ASSIGN_LABEL, labels(h))
        self.assertIn(f"adm_place_admins:{place_id}", callbacks(h))

        await h.callback(f"adm_place_admins:{place_id}", user_id=SUPER_TG)
        self.assertIn("Управление администратором", h.screen_text())
        self.assertIn("Назначаемый Админ", h.screen_text())
        sub = callbacks(h)
        self.assertIn(f"adm_replace:{place_id}", sub)
        self.assertIn(f"adm_revoke:{place_id}", sub)
        self.assertIn("adm_place_admins_back", sub, "нет возврата к карточке")
        self.assertEqual(
            sub[-1], "adm_place_admins_back", "возврат к карточке не последний"
        )
        self.assertNotIn(f"adm_assign:{place_id}", sub)

        # Возврат к карточке.
        await h.callback("adm_place_admins_back", user_id=SUPER_TG)
        self.assertIn(CARD_TITLE, h.screen_text())
        self.assertIn(f"adm_place_admins:{place_id}", callbacks(h))

        # Снятие администратора: снова «Назначить администратора».
        await h.callback(f"adm_place_admins:{place_id}", user_id=SUPER_TG)
        await h.callback(f"adm_revoke:{place_id}", user_id=SUPER_TG)
        await h.callback("adm_confirm", user_id=SUPER_TG)
        self.assertIn(CARD_TITLE, h.screen_text())
        self.assertIn(ASSIGN_LABEL, labels(h), "после снятия нет «Назначить»")
        self.assertNotIn(MANAGE_LABEL, labels(h))
        self.assertIn(f"adm_assign:{place_id}", callbacks(h))

    @scenario_test
    async def test_05b_place_admin_gets_transfer_in_subsection(
        self, h: Harness
    ) -> None:
        place_id = make_place(h, "Своё место", admin_tg=ADMIN_TG)
        await open_place_card(h, place_id)
        await h.callback(f"adm_place_admins:{place_id}", user_id=ADMIN_TG)
        sub = callbacks(h)
        self.assertIn(f"adm_transfer:{place_id}", sub, "админ места не может передать")
        self.assertIn("📤 Передать админку", labels(h))
        self.assertNotIn(f"adm_replace:{place_id}", sub)
        self.assertNotIn(f"adm_revoke:{place_id}", sub)
        self.assertIn("adm_place_admins_back", sub)

    @scenario_test
    async def test_06_stranger_has_no_admin_actions(self, h: Harness) -> None:
        place_id = make_place(h, "Чужое место", admin_tg=ADMIN_TG)
        add_pending_proposal(h, place_id, code="чужое")
        ensure_user(h, STRANGER_TG, full_name="Посторонний")

        await h.text("/admin", user_id=STRANGER_TG)
        self.assertNotIn(CARD_TITLE, h.screen_text())
        text = h.screen_text()
        self.assertNotIn("Управление администратором", text)
        self.assertNotIn(ASSIGN_LABEL, labels(h))
        self.assertNotIn(MANAGE_LABEL, labels(h))
        self.assertNotIn(f"adm_invite:{place_id}", callbacks(h))

        # Прямой callback без прав: отказ, без перерисовки подраздела.
        before = h.screen_text()
        await h.callback(f"adm_place_admins:{place_id}", user_id=STRANGER_TG)
        self.assertTrue(alert(h), "подделанный callback не получил ответ")
        self.assertNotIn("Управление администратором", h.screen_text())
        self.assertEqual(before, h.screen_text())
        self.assertIsNone(load_admin_session(STRANGER_TG))

        # Подменённое состояние: посторонний «на карточке» чужого места.
        save_admin_session(
            STRANGER_TG,
            {"phase": "place", "place_id": place_id},
        )
        await h.callback(f"adm_place_admins:{place_id}", user_id=STRANGER_TG)
        self.assertEqual(alert(h), DENIED_ALERT)
        self.assertNotIn("Управление администратором", h.screen_text())
        self.assertNotIn(f"adm_replace:{place_id}", callbacks(h))
        self.assertNotIn(f"adm_revoke:{place_id}", callbacks(h))

    @scenario_test
    async def test_06b_foreign_place_admin_is_refused(self, h: Harness) -> None:
        own_id = make_place(h, "Своё место", admin_tg=OTHER_ADMIN_TG)
        foreign_id = make_place(h, "Чужое место", admin_tg=ADMIN_TG)
        add_pending_proposal(h, foreign_id, code="чужое_2")
        await open_place_card(h, own_id, tg=OTHER_ADMIN_TG)
        self.assertIn(CARD_TITLE, h.screen_text())

        await h.callback(f"adm_place_admins:{foreign_id}", user_id=OTHER_ADMIN_TG)
        self.assertTrue(alert(h), "чужая карточка не получила отказ")
        self.assertNotIn("Управление администратором", h.screen_text())
        await h.callback(f"adm_brick_new:{foreign_id}", user_id=OTHER_ADMIN_TG)
        self.assertTrue(alert(h), "чужое создание кирпичика не получило отказ")
        self.assertIsNone(load_brick_session(OTHER_ADMIN_TG))

    @scenario_test
    async def test_06c_stranger_denied_on_place_action_callbacks(
        self, h: Harness
    ) -> None:
        place_id = make_place(h, "Чужое место", admin_tg=ADMIN_TG)
        add_pending_proposal(h, place_id, code="чужое_3")
        add_open_conflict(h, place_id, code="чужой_конфликт")
        ensure_user(h, STRANGER_TG, full_name="Посторонний")
        count_before = len(place_bricks(h, place_id))

        await h.text("/admin", user_id=STRANGER_TG)
        # Подделанные callback-и разделов карточки: серверный отказ, без записи.
        await h.callback(f"adm_bricks:{place_id}", user_id=STRANGER_TG)
        self.assertTrue(alert(h), "подделанный «Кирпичики» без ответа")
        await h.callback(f"adm_bprop_place:{place_id}", user_id=STRANGER_TG)
        self.assertTrue(alert(h), "подделанная очередь предложений без ответа")
        await h.callback(f"adm_bconflicts:{place_id}", user_id=STRANGER_TG)
        self.assertTrue(alert(h), "подделанные конфликты без ответа")
        await h.callback(f"adm_invite:{place_id}", user_id=STRANGER_TG)
        self.assertTrue(alert(h), "подделанная ссылка анонса без ответа")
        self.assertEqual(len(place_bricks(h, place_id)), count_before)


# ---------------------------------------------------------------------------
# A4 (§13): локальные кирпичики площадки
# ---------------------------------------------------------------------------


@_skip_if_unavailable
class PlaceBricksAcceptance(unittest.TestCase):
    """П. 7–12: создание локального кирпичика, возврат, минимум сообщений."""

    def setUp(self) -> None:
        self._patchers = [patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,))]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    @scenario_test
    async def test_07_create_button_only_for_place_list(self, h: Harness) -> None:
        place_id = make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await open_place_card(h, place_id)
        await open_place_bricks(h, place_id)

        self.assertIn(CREATE_LABEL, labels(h), f"нет кнопки создания: {labels(h)}")
        self.assertIn(f"adm_brick_new:{place_id}", callbacks(h))
        self.assertIn(f"adm_place:{place_id}", callbacks(h), "«Назад» не в карточку")

        # Системный каталог — без кнопки создания.
        ensure_user(h, SUPER_TG, full_name="Супер Админ")
        await h.callback("main_menu", user_id=ADMIN_TG)
        await h.text("/admin", user_id=SUPER_TG)
        await h.callback("adm_bricks_global", user_id=SUPER_TG)
        self.assertNotIn("adm_brick_new", " ".join(d for d in callbacks(h) if d))
        self.assertNotIn(CREATE_LABEL, labels(h))

    @scenario_test
    async def test_08_local_brick_scope_place_no_proposal(self, h: Harness) -> None:
        place_id = make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await open_place_card(h, place_id)
        await open_place_bricks(h, place_id)
        await h.callback(f"adm_brick_new:{place_id}", user_id=ADMIN_TG)

        # Существующий мастер: шаг названия, а не новый экран.
        cat.assert_shows(
            self, h.screen_text(), "bricks.build.1_nazvat_kirpichik_naprimer_html"
        )
        session_state = load_brick_session(ADMIN_TG)
        assert session_state is not None
        self.assertEqual(session_state["phase"], "title_input")
        self.assertEqual(session_state.get("create_scope"), "place")
        self.assertEqual(session_state.get("create_place_id"), place_id)
        self.assertIsNone(load_admin_session(ADMIN_TG))

        await fill_local_brick(h, title="Ведущий", code="ведущий")

        bricks = place_bricks(h, place_id)
        self.assertEqual(len(bricks), 1, f"кирпичик площадки не создан: {bricks}")
        brick = bricks[0]
        self.assertEqual(brick.scope, "place")
        self.assertEqual(brick.place_id, place_id)
        self.assertIsNone(brick.owner_user_id, "локальный кирпичик стал личным")
        self.assertEqual(brick.status, "active", "кирпичик не активен сразу")
        self.assertEqual(brick.code, "ведущий")
        self.assertEqual(brick.title, "Ведущий")
        self.assertEqual(
            proposal_count(h), 0, "создание породило предложение/согласование"
        )
        # Личных кирпичиков у админа не появилось.
        with h.session_factory() as session:
            personal = session.scalar(
                select(func.count()).select_from(Brick).where(Brick.scope == "personal")
            )
        self.assertEqual(personal, 0, "создан личный кирпичик вместо локального")

    @scenario_test
    async def test_08b_validation_and_duplicate_code_keeps_state(
        self, h: Harness
    ) -> None:
        place_id = make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await open_place_card(h, place_id)
        await open_place_bricks(h, place_id)
        await h.callback(f"adm_brick_new:{place_id}", user_id=ADMIN_TG)
        await h.text("Ведущий", user_id=ADMIN_TG)
        # Некорректный код не должен создавать запись.
        await h.text("   ", user_id=ADMIN_TG)
        self.assertEqual(place_bricks(h, place_id), [])
        state = load_brick_session(ADMIN_TG)
        assert state is not None, "некорректный код сбросил мастер"
        self.assertEqual(state["phase"], "code_input")
        self.assertIn("Ведущий", str(state.get("draft")))

    @scenario_test
    async def test_08c_duplicate_and_double_save_do_not_duplicate(
        self, h: Harness
    ) -> None:
        place_id = make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await open_place_card(h, place_id)
        await open_place_bricks(h, place_id)

        await h.callback(f"adm_brick_new:{place_id}", user_id=ADMIN_TG)
        await fill_local_brick(h, title="Ведущий", code="ведущий")
        self.assertEqual(len(place_bricks(h, place_id)), 1)

        # Повторное нажатие «Сохранить» не создаёт второй кирпичик.
        await h.callback("bk_save", user_id=ADMIN_TG)
        self.assertEqual(
            len(place_bricks(h, place_id)), 1, "двойное сохранение создало дубль"
        )

        # Второй кирпичик с занятым кодом в той же области — отказ, ввод цел.
        await h.callback(f"adm_brick_new:{place_id}", user_id=ADMIN_TG)
        await h.text("Дубль", user_id=ADMIN_TG)
        await h.text("ведущий", user_id=ADMIN_TG)
        self.assertEqual(
            len(place_bricks(h, place_id)), 1, "занятый код создал второй кирпичик"
        )
        state = load_brick_session(ADMIN_TG)
        assert state is not None, "конфликт кода сбросил мастер"
        self.assertEqual(state["phase"], "code_input")

    @scenario_test
    async def test_09_superadmin_can_create_and_stranger_refused(
        self, h: Harness
    ) -> None:
        place_id = make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        ensure_user(h, SUPER_TG, full_name="Супер Админ")
        ensure_user(h, STRANGER_TG, full_name="Посторонний")
        add_pending_proposal(h, place_id, code="чужое_предложение")

        await open_place_card(h, place_id, tg=SUPER_TG)
        await open_place_bricks(h, place_id, tg=SUPER_TG)
        self.assertIn(CREATE_LABEL, labels(h))
        await h.callback(f"adm_brick_new:{place_id}", user_id=SUPER_TG)
        await fill_local_brick(h, title="Супер кирпич", code="супер_код", tg=SUPER_TG)
        codes = [brick.code for brick in place_bricks(h, place_id)]
        self.assertIn("супер_код", codes, "суперадмин не смог создать кирпичик")
        self.assertEqual(proposal_count(h), 1, "журнал предложений изменился")

        # Посторонний: прямой callback отклоняется, записи нет.
        await h.text("/admin", user_id=STRANGER_TG)
        count_before = len(place_bricks(h, place_id))
        await h.callback(f"adm_brick_new:{place_id}", user_id=STRANGER_TG)
        self.assertTrue(alert(h), "посторонний не получил отказ")
        self.assertIsNone(load_brick_session(STRANGER_TG))
        self.assertEqual(len(place_bricks(h, place_id)), count_before)

    @scenario_test
    async def test_10_after_save_returns_to_same_place_list(self, h: Harness) -> None:
        place_id = make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        other_id = make_place(h, "Другое место", admin_tg=ADMIN_TG)
        await open_place_card(h, place_id)
        await open_place_bricks(h, place_id)
        await h.callback(f"adm_brick_new:{place_id}", user_id=ADMIN_TG)
        await fill_local_brick(h, title="Ведущий", code="ведущий")

        text = h.screen_text()
        self.assertIn("Дом культуры", text, "возврат не в список этой площадки")
        self.assertNotIn("Мои кирпичики", text)
        self.assertIn(CREATE_LABEL, labels(h), "в списке нет кнопки создания")
        self.assertIn(f"adm_place:{place_id}", callbacks(h))
        self.assertNotIn("админ", text.lower().split("\n")[0])
        brick_labels = " ".join(label for label, _d in h.screen_buttons()).lower()
        self.assertIn("ведущий", brick_labels, "созданный кирпичик не виден в списке")
        self.assertIsNone(
            load_brick_session(ADMIN_TG), "завершённый мастер перехватывает ввод"
        )
        # Список другой площадки не затронут.
        self.assertEqual(place_bricks(h, other_id), [])

    @scenario_test
    async def test_09b_place_missing_and_cancel_are_safe(self, h: Harness) -> None:
        place_id = make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await open_place_card(h, place_id)
        await open_place_bricks(h, place_id)

        # Отмена мастера с предпросмотра: кирпичик не сохранён, возврат в список.
        await h.callback(f"adm_brick_new:{place_id}", user_id=ADMIN_TG)
        await h.text("Черновик", user_id=ADMIN_TG)
        await h.text("черновик_код", user_id=ADMIN_TG)
        await h.callback("bk_skip_description", user_id=ADMIN_TG)
        await h.callback("bk_type:short_text", user_id=ADMIN_TG)
        await h.callback("bk_cancel", user_id=ADMIN_TG)
        self.assertEqual(place_bricks(h, place_id), [], "отмена сохранила кирпичик")
        self.assertIn("Дом культуры", h.screen_text())
        self.assertIn(f"adm_place:{place_id}", callbacks(h))
        self.assertIsNone(load_brick_session(ADMIN_TG))

        # Несуществующая площадка: понятный alert, мастер не открыт.
        await h.callback("adm_brick_new:999999", user_id=ADMIN_TG)
        self.assertIn("не найдено", alert(h).lower())
        self.assertIsNone(load_brick_session(ADMIN_TG))

        # Повторный (устаревший) callback из закрытого кабинета — отказ.
        await open_place_bricks(h, place_id)
        await h.callback("main_menu", user_id=ADMIN_TG)
        await h.callback(f"adm_brick_new:{place_id}", user_id=ADMIN_TG)
        self.assertTrue(alert(h), "устаревшая кнопка без ответа")
        self.assertIsNone(load_brick_session(ADMIN_TG))

    @scenario_test
    async def test_11_back_from_place_bricks_and_after_restart(
        self, h: Harness
    ) -> None:
        place_id = make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await open_place_card(h, place_id)
        await open_place_bricks(h, place_id)
        back = [
            data for _l, data in h.screen_buttons() if data == f"adm_place:{place_id}"
        ]
        self.assertEqual(len(back), 1, "«Назад» не ведёт в карточку места")

        await h.callback(back[0], user_id=ADMIN_TG)
        cat.assert_not_stale(self, alert(h), "рабочая кнопка дала stale")
        self.assertIn(CARD_TITLE, h.screen_text())
        state = load_admin_session(ADMIN_TG)
        assert state is not None
        self.assertEqual(state["phase"], "place")
        self.assertEqual(state["place_id"], place_id)

        # Рестарт процесса: старое сообщение/кнопки остаются в чате.
        await open_place_bricks(h, place_id)
        await h.restart()
        await h.callback(f"adm_place:{place_id}", user_id=ADMIN_TG)
        cat.assert_not_stale(self, alert(h), "после рестарта «Назад» даёт stale")
        self.assertIn(CARD_TITLE, h.screen_text())
        state = load_admin_session(ADMIN_TG)
        assert state is not None
        self.assertEqual(state["phase"], "place")
        self.assertEqual(state["place_id"], place_id)

        # И повторный вход в кирпичики после рестарта, затем «Назад».
        await h.callback(f"adm_bricks:{place_id}", user_id=ADMIN_TG)
        self.assertIn(CREATE_LABEL, labels(h))
        await h.callback(f"adm_place:{place_id}", user_id=ADMIN_TG)
        cat.assert_not_stale(self, alert(h))
        self.assertIn(CARD_TITLE, h.screen_text())

    @scenario_test
    async def test_12_navigation_uses_one_service_screen(self, h: Harness) -> None:
        place_id = make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await open_place_card(h, place_id)
        card_id = screen_id(h, ADMIN_TG)
        self.assertIsNotNone(card_id)
        sends_before = len(screen_message_ids(h))

        await open_place_bricks(h, place_id)
        self.assertEqual(
            screen_id(h, ADMIN_TG), card_id, "список кирпичиков создал новый экран"
        )
        self.assertEqual(
            len(screen_message_ids(h)), sends_before, "список — отдельное сообщение"
        )

        await h.callback(f"adm_brick_new:{place_id}", user_id=ADMIN_TG)
        self.assertEqual(
            screen_id(h, ADMIN_TG), card_id, "мастер создания создал новый экран"
        )
        self.assertEqual(
            len(screen_message_ids(h)), sends_before, "мастер — отдельное сообщение"
        )

        # Возврат в список и в карточку — та же служебная поверхность.
        await h.callback("bk_back", user_id=ADMIN_TG)
        self.assertEqual(screen_id(h, ADMIN_TG), card_id)
        await h.callback(f"adm_place:{place_id}", user_id=ADMIN_TG)
        self.assertEqual(screen_id(h, ADMIN_TG), card_id)
        self.assertEqual(len(screen_message_ids(h)), sends_before)
        self.assertIn(card_id, edited_message_ids(h))

    @scenario_test
    async def test_12b_save_returns_without_message_flood(self, h: Harness) -> None:
        place_id = make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await open_place_card(h, place_id)
        await open_place_bricks(h, place_id)
        await h.callback(f"adm_brick_new:{place_id}", user_id=ADMIN_TG)
        await fill_local_brick(h, title="Ведущий", code="ведущий")

        # Текстовый ввод пересоздаёт экран рядом с вводом (Stage 16), но
        # каждое нажатие кнопки не создаёт новое сообщение.
        self.assertLessEqual(
            len(screen_message_ids(h)),
            3,
            f"навигация размножила экраны: {screen_message_ids(h)}",
        )
        self.assertIn(
            f"adm_brick_new:{place_id}", " ".join(d for d in callbacks(h) if d)
        )
        # «Назад» после сохранения — в карточку, без stale.
        await h.callback(f"adm_place:{place_id}", user_id=ADMIN_TG)
        cat.assert_not_stale(self, alert(h))
        self.assertIn(CARD_TITLE, h.screen_text())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
