# tests/test_stage25_a4_place_bricks.py
"""
Poster 2.5 §13: локальные кирпичики площадки (A4).

Проверяется пользовательский контракт, а не реализация:

1. «➕ Создать кирпичик» в `Площадка → Кирпичики` открывает существующий
   конструктор; сохранённый кирпичик сразу принадлежит площадке
   (`scope=place`, верный `place_id`, не личный, не предложение).
2. Посторонний не попадает в конструктор даже с подделанным callback.
3. После сохранения — возврат к списку кирпичиков ТОЙ ЖЕ площадки, а не в
   «Мои кирпичики».
4. «◀️ Назад» из списка кирпичиков площадки ведёт в административную
   карточку (`adm_place:<id>`), в том числе после рестарта процесса:
   stale-alert «Эта кнопка больше не работает…» не появляется.

Все сценарии идут через настоящее `Application.process_update`, FakeBot и
временную SQLite (`tests.integration_harness`). Реальная база и сеть
Telegram не используются.

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage25_a4_place_bricks -v
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import func, select

from admin_state import load_admin_session
from brick_state import load_brick_session
from models import Brick, BrickProposal, PlaceAdmin, utcnow
from place_store import create_place
from tests.integration_harness import (
    DEFAULT_USER_ID,
    HARNESS_IMPORT_ERROR,
    Harness,
    scenario_test,
)
from user_store import get_or_create_user

ADMIN_TG = 2_504_001
OTHER_ADMIN_TG = 2_504_002
STRANGER_TG = 2_504_003

CREATE_LABEL = "➕ Создать кирпичик"
STALE_ALERT = "эта кнопка больше не работает"


# --- помощники ---------------------------------------------------------------


def make_place(
    h: Harness,
    name: str,
    *,
    admin_tg: int | None = None,
) -> int:
    with h.session_factory() as session:
        admin = None
        if admin_tg is not None:
            admin = get_or_create_user(
                session, admin_tg, username=f"a{admin_tg}", full_name="Админ места"
            )
        place = create_place(
            session,
            name,
            city="Город приёмки",
            created_by_user_id=admin.id if admin is not None else None,
        )
        if admin is not None:
            session.add(
                PlaceAdmin(
                    place_id=place.id,
                    user_id=admin.id,
                    assigned_at=utcnow(),
                )
            )
            session.commit()
        return int(place.id)


def buttons(h: Harness) -> list[tuple[str, str | None]]:
    return list(h.screen_buttons())


def callbacks(h: Harness) -> list[str | None]:
    return [data for _label, data in buttons(h)]


def last_alert(h: Harness) -> str:
    answers = h.answers()
    if not answers:
        return ""
    record = answers[-1]
    args = tuple(record.get("args") or ())
    if args and isinstance(args[0], str):
        return args[0]
    return str(record.get("text") or "")


def place_bricks(h: Harness, place_id: int) -> list[Brick]:
    with h.session_factory() as session:
        return list(
            session.scalars(
                select(Brick)
                .where(Brick.scope == "place", Brick.place_id == place_id)
                .order_by(Brick.id)
            ).all()
        )


async def open_place_bricks(h: Harness, place_id: int, *, tg: int = ADMIN_TG) -> None:
    await h.text("/admin", user_id=tg)
    await h.callback(f"adm_place:{place_id}", user_id=tg)
    await h.callback(f"adm_bricks:{place_id}", user_id=tg)


async def fill_local_brick(
    h: Harness,
    *,
    title: str,
    code: str,
    tg: int = ADMIN_TG,
) -> None:
    """Полный путь мастера создания локального кирпичика до сохранения."""
    await h.text(title, user_id=tg)
    await h.text(code, user_id=tg)
    await h.callback("bk_skip_description", user_id=tg)
    await h.callback("bk_type:short_text", user_id=tg)
    await h.callback("bk_save", user_id=tg)


# --- тесты -------------------------------------------------------------------


@unittest.skipIf(
    HARNESS_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {HARNESS_IMPORT_ERROR}",
)
class PlaceBrickCreateTests(unittest.TestCase):
    """§13: создание локального кирпичика из раздела площадки."""

    @scenario_test
    async def test_create_button_opens_existing_wizard(self, h: Harness) -> None:
        place_id = make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await open_place_bricks(h, place_id)

        self.assertIn(CREATE_LABEL, [label for label, _ in buttons(h)])
        self.assertIn(f"adm_brick_new:{place_id}", callbacks(h))

        await h.callback(f"adm_brick_new:{place_id}", user_id=ADMIN_TG)
        # Существующий конструктор: первый шаг мастера, а не новый экран.
        self.assertIn("Как назвать кирпичик", h.screen_text())
        state = load_brick_session(ADMIN_TG)
        self.assertIsNotNone(state, "мастер не сохранён в brick_sessions")
        assert state is not None
        self.assertEqual(state["phase"], "title_input")
        self.assertEqual(state["mode"], "create")
        self.assertEqual(state.get("create_scope"), "place")
        self.assertEqual(state.get("create_place_id"), place_id)

    @scenario_test
    async def test_saved_brick_belongs_to_place(self, h: Harness) -> None:
        place_id = make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await open_place_bricks(h, place_id)
        await h.callback(f"adm_brick_new:{place_id}", user_id=ADMIN_TG)
        await fill_local_brick(h, title="Ведущий", code="ведущий")

        rows = place_bricks(h, place_id)
        self.assertEqual(len(rows), 1, f"кирпичик площадки не создан: {rows}")
        brick = rows[0]
        self.assertEqual(brick.scope, "place")
        self.assertEqual(brick.place_id, place_id)
        self.assertIsNone(
            brick.owner_user_id, "локальный кирпичик не должен быть личным"
        )
        self.assertEqual(brick.title, "Ведущий")
        self.assertEqual(brick.code, "ведущий")
        self.assertEqual(brick.value_type, "short_text")
        # Не предложение: журнал предложений пуст.
        with h.session_factory() as session:
            proposals = session.scalar(select(func.count()).select_from(BrickProposal))
        self.assertEqual(proposals, 0, "создание не должно порождать предложение")

    @scenario_test
    async def test_after_save_returns_to_same_place_list(self, h: Harness) -> None:
        place_id = make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await open_place_bricks(h, place_id)
        await h.callback(f"adm_brick_new:{place_id}", user_id=ADMIN_TG)
        await fill_local_brick(h, title="Ведущий", code="ведущий")

        text = h.screen_text()
        self.assertIn("создан", text)
        self.assertIn("Дом культуры", text, "возврат не в список этой площадки")
        self.assertNotIn("Мои кирпичики", text, "возврат ушёл в личные кирпичики")
        self.assertIn(f"adm_place:{place_id}", callbacks(h))
        state = load_admin_session(ADMIN_TG)
        assert state is not None
        self.assertEqual(state["phase"], "brick_list")
        self.assertEqual(state["brick_scope"], "place")
        self.assertEqual(state["brick_place_id"], place_id)
        self.assertIsNone(
            load_brick_session(ADMIN_TG),
            "завершённый мастер не должен перехватывать ввод",
        )
        # Созданный кирпичик виден в списке площадки.
        self.assertIn("ведущий", " ".join(label for label, _ in buttons(h)).lower())

    @scenario_test
    async def test_stranger_cannot_open_wizard(self, h: Harness) -> None:
        place_id = make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await open_place_bricks(h, place_id)
        await h.callback("main_menu", user_id=ADMIN_TG)

        # Посторонний не имеет доступа к кабинету вовсе.
        await h.text("/admin", user_id=STRANGER_TG)
        await h.callback(f"adm_brick_new:{place_id}", user_id=STRANGER_TG)
        self.assertTrue(last_alert(h), "подделанный callback должен получить alert")
        self.assertIsNone(
            load_brick_session(STRANGER_TG), "посторонний попал в конструктор"
        )
        self.assertEqual(place_bricks(h, place_id), [])

    @scenario_test
    async def test_foreign_place_admin_cannot_open_wizard(self, h: Harness) -> None:
        own_id = make_place(h, "Своё место", admin_tg=OTHER_ADMIN_TG)
        foreign_id = make_place(h, "Чужое место", admin_tg=ADMIN_TG)
        await open_place_bricks(h, own_id, tg=OTHER_ADMIN_TG)

        await h.callback(f"adm_brick_new:{foreign_id}", user_id=OTHER_ADMIN_TG)
        self.assertTrue(last_alert(h), "чужая площадка должна получить отказ")
        self.assertIsNone(load_brick_session(OTHER_ADMIN_TG))


@unittest.skipIf(
    HARNESS_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {HARNESS_IMPORT_ERROR}",
)
class PlaceBrickBackTests(unittest.TestCase):
    """§13: «◀️ Назад» из кирпичиков площадки → административная карточка."""

    @scenario_test
    async def test_back_returns_to_admin_card(self, h: Harness) -> None:
        place_id = make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await open_place_bricks(h, place_id)
        self.assertIn(
            f"adm_place:{place_id}",
            callbacks(h),
            f"кнопка «Назад» не ведёт в карточку: {buttons(h)}",
        )

        await h.callback(f"adm_place:{place_id}", user_id=ADMIN_TG)
        self.assertNotIn(STALE_ALERT, last_alert(h).lower())
        self.assertIn("Место", h.screen_text())
        state = load_admin_session(ADMIN_TG)
        assert state is not None
        self.assertEqual(state["phase"], "place")
        self.assertEqual(state["place_id"], place_id)

    @scenario_test
    async def test_back_works_after_restart(self, h: Harness) -> None:
        place_id = make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await open_place_bricks(h, place_id)
        self.assertEqual(
            (load_admin_session(ADMIN_TG) or {}).get("phase"), "brick_list"
        )

        await h.restart()
        await h.callback(f"adm_place:{place_id}", user_id=ADMIN_TG)

        self.assertNotIn(
            STALE_ALERT,
            last_alert(h).lower(),
            "после рестарта рабочая кнопка отвечает stale-alert",
        )
        self.assertIn("Место", h.screen_text())
        state = load_admin_session(ADMIN_TG)
        assert state is not None
        self.assertEqual(state["phase"], "place")
        self.assertEqual(state["place_id"], place_id)

    @scenario_test
    async def test_back_from_local_brick_card_returns_to_place_list(
        self, h: Harness
    ) -> None:
        place_id = make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await open_place_bricks(h, place_id)
        await h.callback(f"adm_brick_new:{place_id}", user_id=ADMIN_TG)
        await fill_local_brick(h, title="Ведущий", code="ведущий")

        brick_id = place_bricks(h, place_id)[0].id
        await h.callback(f"adm_bedit:{brick_id}", user_id=ADMIN_TG)
        await h.callback("adm_bedit_back", user_id=ADMIN_TG)
        self.assertIn("Дом культуры", h.screen_text())
        self.assertNotIn(STALE_ALERT, last_alert(h).lower())

        # И уже из списка — в административную карточку.
        await h.callback(f"adm_place:{place_id}", user_id=ADMIN_TG)
        self.assertIn("Место", h.screen_text())
        self.assertNotIn(STALE_ALERT, last_alert(h).lower())

    @scenario_test
    async def test_wizard_back_returns_to_place_list(self, h: Harness) -> None:
        place_id = make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await open_place_bricks(h, place_id)
        await h.callback(f"adm_brick_new:{place_id}", user_id=ADMIN_TG)

        await h.callback("bk_back", user_id=ADMIN_TG)
        self.assertIn("Дом культуры", h.screen_text())
        self.assertNotIn("Мои кирпичики", h.screen_text())
        state = load_admin_session(ADMIN_TG)
        assert state is not None
        self.assertEqual(state["phase"], "brick_list")
        self.assertIsNone(load_brick_session(ADMIN_TG))
        self.assertEqual(place_bricks(h, place_id), [])

    @scenario_test
    async def test_place_brick_list_has_no_create_for_system_catalog(
        self, h: Harness
    ) -> None:
        with patch("config.SUPERADMIN_USER_IDS", (DEFAULT_USER_ID,)):
            await h.text("/admin", user_id=DEFAULT_USER_ID)
            await h.callback("adm_bricks_global", user_id=DEFAULT_USER_ID)

        self.assertNotIn(
            "adm_brick_new",
            " ".join(data or "" for data in callbacks(h)),
            "кнопка создания не должна появляться в системном каталоге",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
