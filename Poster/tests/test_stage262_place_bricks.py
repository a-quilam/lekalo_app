# tests/test_stage262_place_bricks.py
"""
Poster 2.6.2 §3.1–3.2: системные кирпичики площадки и локальные описания.

Контракт:

1. экран «🧩 Системные кирпичики» в административной карточке места:
   живой системный каталог, пять записей на страницу, пагинация 0/1/5/6/11;
2. карточка кирпичика показывает ДЕЙСТВУЮЩЕЕ описание — локальное
   переопределение площадки, если оно есть, иначе описание каталога;
3. локальное описание хранится отдельно (`place_brick_descriptions`),
   не меняет каталог и другие места, переживает рестарт;
4. пустой текст и кнопка «↩️ Общее описание» возвращают описание каталога;
5. мастер анонса показывает локальное описание площадки (вопросы и поля);
6. права проверяются по фактическим строкам БД на каждом действии:
   посторонний, чужое место и архивный/удалённый кирпичик получают отказ;
7. «◀️ Назад» работает на всех шагах, в том числе после рестарта;
8. состояние сценария «Места» (`place_sessions`) не затрагивается;
9. проигранная гонка записи (`stale`) не оставляет мусора в БД.

Все сценарии идут через настоящее `Application.process_update`, FakeBot и
временную SQLite (`tests.integration_harness`); рабочая база, `.env` и сеть
Telegram не используются.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage262_place_bricks -v
"""

from __future__ import annotations

import unittest
from unittest import mock

from sqlalchemy import func, select

import place_brick_store
from models import Brick, PlaceAdmin, PlaceBrickDescription, utcnow
from place_brick_store import (
    get_place_brick_view,
    set_place_brick_description,
)
from tests import ui_texts_expect as cat
from tests.integration_harness import (
    DEFAULT_USER_ID,
    HARNESS_IMPORT_ERROR,
    Harness,
    scenario_test,
)
from user_store import get_or_create_user

ADMIN_TG = 2_620_001
OTHER_ADMIN_TG = 2_620_002
STRANGER_TG = 2_620_003

LIST_LABEL = "🧩 Системные кирпичики"
EDIT_LABEL = "✏️ Своё описание"
RESET_LABEL = "↩️ Общее описание"

# Системный кирпичик «название» (id 1): вопрос мастера показывает его описание.
NAME_BRICK_ID = 1


# --- помощники ---------------------------------------------------------------


def _make_place(
    h: Harness,
    name: str,
    *,
    admin_tg: int | None = None,
) -> int:
    """Место с действующим администратором (или без него)."""
    with h.session_factory() as session:
        user = None
        if admin_tg is not None:
            user = get_or_create_user(
                session, admin_tg, username=f"pb{admin_tg}", full_name="Админ места"
            )
        from place_store import create_place

        place = create_place(
            session,
            name,
            city="Город приёмки",
            created_by_user_id=user.id if user is not None else None,
        )
        if user is not None:
            session.add(
                PlaceAdmin(place_id=place.id, user_id=user.id, assigned_at=utcnow())
            )
            session.commit()
        return int(place.id)


def _ensure_user(h: Harness, telegram_user_id: int) -> None:
    with h.session_factory() as session:
        get_or_create_user(
            session,
            telegram_user_id,
            username=f"pb{telegram_user_id}",
            full_name="Пользователь",
        )
        session.commit()


def _system_bricks(h: Harness) -> list[Brick]:
    with h.session_factory() as session:
        return list(
            session.scalars(
                select(Brick).where(Brick.scope == "system").order_by(Brick.id)
            ).all()
        )


def _set_active_system_count(h: Harness, count: int) -> None:
    """Оставляет активными первые `count` системных кирпичиков, прочие — в архив."""
    with h.session_factory() as session:
        rows = list(
            session.scalars(
                select(Brick).where(Brick.scope == "system").order_by(Brick.id)
            ).all()
        )
        for index, row in enumerate(rows):
            row.status = "active" if index < count else "archived"
        session.commit()


def _rows(h: Harness) -> list[list[tuple[str, str | None]]]:
    record = h.screen()
    assert record is not None, "экрана ещё не было"
    markup = record.get("reply_markup")
    assert markup is not None, "у экрана нет клавиатуры"
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


def _callbacks(h: Harness) -> list[str | None]:
    return [data for _label, data in h.screen_buttons()]


def _last_alert(h: Harness) -> str:
    answers = h.answers()
    if not answers:
        return ""
    record = answers[-1]
    args = tuple(record.get("args") or ())
    if args and isinstance(args[0], str):
        return args[0]
    return str(record.get("text") or "")


def _active_system_bricks(h: Harness) -> list[Brick]:
    return [row for row in _system_bricks(h) if row.status == "active"]


def _place_description_rows(h: Harness, place_id: int | None = None) -> int:
    with h.session_factory() as session:
        statement = select(func.count()).select_from(PlaceBrickDescription)
        if place_id is not None:
            statement = statement.where(PlaceBrickDescription.place_id == place_id)
        return int(session.scalar(statement) or 0)


def _set_local_description(
    h: Harness,
    *,
    place_id: int,
    brick_id: int,
    text: str,
    actor_tg: int = ADMIN_TG,
) -> None:
    with h.session_factory() as session:
        set_place_brick_description(
            session,
            actor_telegram_user_id=actor_tg,
            place_id=place_id,
            brick_id=brick_id,
            description=text,
        )


async def _open_place_card(h: Harness, place_id: int, *, tg: int = ADMIN_TG) -> None:
    await h.text("/admin", user_id=tg)
    await h.callback(f"adm_place:{place_id}", user_id=tg)


async def _open_system_bricks(h: Harness, place_id: int, *, tg: int = ADMIN_TG) -> None:
    await _open_place_card(h, place_id, tg=tg)
    await h.callback(f"adm_pbricks:{place_id}", user_id=tg)


# --- §3.1: список системных кирпичиков ---------------------------------------


@unittest.skipIf(
    HARNESS_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {HARNESS_IMPORT_ERROR}",
)
class PlaceSystemBricksListTests(unittest.TestCase):
    """Пункт 1: экран, пять записей на страницу, пагинация."""

    @scenario_test
    async def test_entry_button_and_first_page(self, h: Harness) -> None:
        place_id = _make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await _open_place_card(h, place_id)

        self.assertIn(f"adm_pbricks:{place_id}", _callbacks(h))
        self.assertIn(LIST_LABEL, [label for label, _d in h.screen_buttons()])

        await h.callback(f"adm_pbricks:{place_id}", user_id=ADMIN_TG)
        cat.assert_shows(self, h.screen_text(), "admin.place_bricks.spisok_html")
        cat.assert_not_stale(self, _last_alert(h))

        brick_rows = [
            row
            for row in _rows(h)
            if len(row) == 1 and (row[0][1] or "").startswith("adm_pbrick:")
        ]
        active = _active_system_bricks(h)
        self.assertEqual(len(active), 13, "ожидался полный системный каталог")
        self.assertEqual(len(brick_rows), 5, "на странице должно быть пять записей")
        # Навигация есть, потому что записей больше пяти.
        nav = [
            row
            for row in _rows(h)
            if any((data or "").startswith("adm_pbricks_page:") for _l, data in row)
        ]
        self.assertEqual(len(nav), 1, "нет строки пагинации")

    @scenario_test
    async def test_pagination_boundaries(self, h: Harness) -> None:
        """0/1/5 записей — без пагинации; 6 — две страницы; 11 — три."""
        place_id = _make_place(h, "Дом культуры", admin_tg=ADMIN_TG)

        async def open_list() -> None:
            await h.text("/admin", user_id=ADMIN_TG)
            await h.callback(f"adm_place:{place_id}", user_id=ADMIN_TG)
            await h.callback(f"adm_pbricks:{place_id}", user_id=ADMIN_TG)

        def nav_rows() -> list[list[tuple[str, str | None]]]:
            return [
                row
                for row in _rows(h)
                if any(
                    (data or "").startswith(("adm_pbricks_page:", "noop:"))
                    for _l, data in row
                )
            ]

        def brick_rows() -> list[list[tuple[str, str | None]]]:
            return [
                row
                for row in _rows(h)
                if len(row) == 1 and (row[0][1] or "").startswith("adm_pbrick:")
            ]

        for count in (0, 1, 5):
            with self.subTest(count=count):
                _set_active_system_count(h, count)
                await open_list()
                self.assertEqual(len(brick_rows()), count, f"записей: {count}")
                self.assertEqual(nav_rows(), [], f"лишняя пагинация при {count}")
                if count == 0:
                    cat.assert_shows(
                        self, h.screen_text(), "admin.place_bricks.net_kirpichikov"
                    )

        with self.subTest(count=6):
            _set_active_system_count(h, 6)
            await open_list()
            self.assertEqual(len(brick_rows()), 5)
            self.assertTrue(nav_rows(), "нет навигации при шести записях")
            far = [
                data
                for _l, data in nav_rows()[0]
                if (data or "").startswith("adm_pbricks_page:")
            ]
            self.assertEqual(far, ["adm_pbricks_page:5"], f"стрелки: {nav_rows()}")

        with self.subTest(count=11):
            _set_active_system_count(h, 11)
            await open_list()
            self.assertEqual(len(brick_rows()), 5)
            await h.callback("adm_pbricks_page:10", user_id=ADMIN_TG)
            self.assertEqual(len(brick_rows()), 1, "на третьей странице одна запись")
            far = [
                data
                for row in nav_rows()
                for _l, data in row
                if (data or "").startswith("adm_pbricks_page:")
            ]
            self.assertEqual(far, ["adm_pbricks_page:5"], f"стрелки: {far}")

    @scenario_test
    async def test_local_description_marker_in_list(self, h: Harness) -> None:
        place_id = _make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        _set_local_description(
            h, place_id=place_id, brick_id=NAME_BRICK_ID, text="Своё пояснение"
        )
        await _open_system_bricks(h, place_id)

        marked = [
            label
            for label, _data in h.screen_buttons()
            if (label or "").endswith(cat.text("admin.place_bricks.svoe_opisanie"))
        ]
        self.assertTrue(
            marked, f"нет отметки локального описания: {h.screen_buttons()}"
        )


# --- §3.2: карточка и запись локального описания -----------------------------


@unittest.skipIf(
    HARNESS_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {HARNESS_IMPORT_ERROR}",
)
class PlaceBrickDescriptionCardTests(unittest.TestCase):
    """Пункты 2–4: действующее описание, изоляция, возврат к каталогу."""

    @scenario_test
    async def test_card_shows_catalog_description_first(self, h: Harness) -> None:
        place_id = _make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await _open_system_bricks(h, place_id)
        await h.callback(f"adm_pbrick:{NAME_BRICK_ID}", user_id=ADMIN_TG)

        text = h.screen_text()
        cat.assert_shows(self, text, "admin.place_brick_card.kartochka_html")
        cat.assert_shows(self, text, "admin.place_brick_card.istochnik_katalog")
        cat.assert_not_shows(self, text, "admin.place_brick_card.istochnik_mesto")
        labels = [label for label, _d in h.screen_buttons()]
        self.assertIn(EDIT_LABEL, labels)
        self.assertNotIn(RESET_LABEL, labels)

    @scenario_test
    async def test_saved_override_changes_card_and_stays_local(
        self, h: Harness
    ) -> None:
        own = _make_place(h, "Своё место", admin_tg=ADMIN_TG)
        other = _make_place(h, "Чужое место", admin_tg=OTHER_ADMIN_TG)
        await _open_system_bricks(h, own)
        await h.callback(f"adm_pbrick:{NAME_BRICK_ID}", user_id=ADMIN_TG)
        await h.callback(f"adm_pbrick_desc:{NAME_BRICK_ID}", user_id=ADMIN_TG)
        await h.text("Напишите название площадки и жанр", user_id=ADMIN_TG)

        text = h.screen_text()
        cat.assert_shows(self, text, "admin.place_brick_card.kartochka_html")
        self.assertIn("Напишите название площадки и жанр", text)
        cat.assert_shows(self, text, "admin.place_brick_card.istochnik_mesto")
        self.assertIn(RESET_LABEL, [label for label, _d in h.screen_buttons()])

        # Каталог не изменён, чужое место видит общее описание.
        with h.session_factory() as session:
            catalog = session.get(Brick, NAME_BRICK_ID)
            assert catalog is not None
            other_view = get_place_brick_view(session, other, NAME_BRICK_ID)
        self.assertNotIn("Напишите название площадки и жанр", catalog.description or "")
        assert other_view is not None
        self.assertFalse(other_view.is_local)
        self.assertEqual(_place_description_rows(h, other), 0)

    @scenario_test
    async def test_empty_text_and_reset_button_restore_catalog(
        self, h: Harness
    ) -> None:
        place_id = _make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        _set_local_description(
            h, place_id=place_id, brick_id=NAME_BRICK_ID, text="Своё пояснение"
        )
        await _open_system_bricks(h, place_id)
        await h.callback(f"adm_pbrick:{NAME_BRICK_ID}", user_id=ADMIN_TG)

        await h.callback(f"adm_pbrick_reset:{NAME_BRICK_ID}", user_id=ADMIN_TG)
        cat.assert_shows(
            self, h.screen_text(), "admin.place_brick_card.istochnik_katalog"
        )
        self.assertEqual(_place_description_rows(h), 0)

        # Пустой текст — та же операция «вернуть общее описание».
        _set_local_description(
            h, place_id=place_id, brick_id=NAME_BRICK_ID, text="Своё пояснение"
        )
        await h.callback(f"adm_pbrick_desc:{NAME_BRICK_ID}", user_id=ADMIN_TG)
        await h.text("   ", user_id=ADMIN_TG)
        cat.assert_shows(
            self, h.screen_text(), "admin.place_brick_card.istochnik_katalog"
        )
        self.assertEqual(_place_description_rows(h), 0)

    @scenario_test
    async def test_override_survives_restart(self, h: Harness) -> None:
        place_id = _make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await _open_system_bricks(h, place_id)
        await h.callback(f"adm_pbrick:{NAME_BRICK_ID}", user_id=ADMIN_TG)
        await h.callback(f"adm_pbrick_desc:{NAME_BRICK_ID}", user_id=ADMIN_TG)
        await h.text("Описание после рестарта", user_id=ADMIN_TG)

        await h.restart()
        await h.text("/admin", user_id=ADMIN_TG)
        await h.callback(f"adm_place:{place_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_pbricks:{place_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_pbrick:{NAME_BRICK_ID}", user_id=ADMIN_TG)

        cat.assert_not_stale(self, _last_alert(h))
        self.assertIn("Описание после рестарта", h.screen_text())
        cat.assert_shows(
            self, h.screen_text(), "admin.place_brick_card.istochnik_mesto"
        )


# --- навигация «Назад» -------------------------------------------------------


@unittest.skipIf(
    HARNESS_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {HARNESS_IMPORT_ERROR}",
)
class PlaceBrickBackTests(unittest.TestCase):
    """Пункт 7: «Назад» на всех шагах, включая возврат после рестарта."""

    @scenario_test
    async def test_back_chain_card_list_place(self, h: Harness) -> None:
        place_id = _make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await _open_system_bricks(h, place_id)
        await h.callback(f"adm_pbrick:{NAME_BRICK_ID}", user_id=ADMIN_TG)

        await h.callback("adm_pbrick_back", user_id=ADMIN_TG)
        cat.assert_not_stale(self, _last_alert(h))
        cat.assert_shows(self, h.screen_text(), "admin.place_bricks.spisok_html")

        # В списке «◀️ Назад» — это переход в карточку ЭТОЙ площадки;
        # нажимаем фактическую кнопку экрана, а не внутренний callback.
        back_callbacks = [
            data
            for _label, data in h.screen_buttons()
            if (data or "") == f"adm_place:{place_id}"
        ]
        self.assertTrue(
            back_callbacks, f"нет возврата в карточку: {h.screen_buttons()}"
        )
        await h.callback(f"adm_place:{place_id}", user_id=ADMIN_TG)
        cat.assert_not_stale(self, _last_alert(h))
        cat.assert_shows(self, h.screen_text(), "admin.place.mesto")

    @scenario_test
    async def test_back_from_input_returns_to_card(self, h: Harness) -> None:
        place_id = _make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await _open_system_bricks(h, place_id)
        await h.callback(f"adm_pbrick:{NAME_BRICK_ID}", user_id=ADMIN_TG)
        await h.callback(f"adm_pbrick_desc:{NAME_BRICK_ID}", user_id=ADMIN_TG)
        cat.assert_shows(self, h.screen_text(), "admin.place_brick_input.opisanie_html")

        await h.callback(f"adm_pbrick:{NAME_BRICK_ID}", user_id=ADMIN_TG)
        cat.assert_not_stale(self, _last_alert(h))
        cat.assert_shows(self, h.screen_text(), "admin.place_brick_card.kartochka_html")

    @scenario_test
    async def test_back_works_after_restart(self, h: Harness) -> None:
        place_id = _make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await _open_system_bricks(h, place_id)
        await h.callback(f"adm_pbrick:{NAME_BRICK_ID}", user_id=ADMIN_TG)
        await h.restart()

        await h.callback("adm_pbrick_back", user_id=ADMIN_TG)
        cat.assert_not_stale(self, _last_alert(h))
        cat.assert_shows(self, h.screen_text(), "admin.place_bricks.spisok_html")


# --- права и безопасные отказы ----------------------------------------------


@unittest.skipIf(
    HARNESS_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {HARNESS_IMPORT_ERROR}",
)
class PlaceBrickAccessTests(unittest.TestCase):
    """Пункт 6: подделанные callback и посторонние не меняют данные."""

    @scenario_test
    async def test_stranger_and_foreign_admin_are_denied(self, h: Harness) -> None:
        place_id = _make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        foreign = _make_place(h, "Чужое место", admin_tg=OTHER_ADMIN_TG)
        _ensure_user(h, STRANGER_TG)
        _set_local_description(
            h,
            place_id=foreign,
            brick_id=NAME_BRICK_ID,
            text="Чужое описание",
            actor_tg=OTHER_ADMIN_TG,
        )

        # Посторонний не имеет доступа к кабинету: подделанный callback
        # получает отказ, а не экран системного каталога.
        await h.text("/admin", user_id=STRANGER_TG)
        await h.callback(f"adm_pbricks:{place_id}", user_id=STRANGER_TG)
        self.assertTrue(_last_alert(h), "посторонний получил экран вместо отказа")
        self.assertFalse(
            [d for _l, d in h.screen_buttons() if (d or "").startswith("adm_pbrick:")],
            "посторонний увидел системный каталог",
        )

        await h.text("/admin", user_id=OTHER_ADMIN_TG)
        await h.callback(f"adm_place:{foreign}", user_id=OTHER_ADMIN_TG)
        await h.callback(f"adm_pbricks:{place_id}", user_id=OTHER_ADMIN_TG)
        self.assertTrue(_last_alert(h), "чужое место открылось по подделанному id")
        self.assertNotIn("Дом культуры", h.screen_text())

        await h.callback(f"adm_pbrick_desc:{NAME_BRICK_ID}", user_id=OTHER_ADMIN_TG)
        await h.text("Подмена", user_id=OTHER_ADMIN_TG)
        with h.session_factory() as session:
            row = session.scalar(
                select(PlaceBrickDescription).where(
                    PlaceBrickDescription.place_id == foreign
                )
            )
        assert row is not None
        self.assertEqual(row.description, "Чужое описание", "чужое описание изменено")

    @scenario_test
    async def test_forged_callback_for_foreign_place_is_denied(
        self, h: Harness
    ) -> None:
        own = _make_place(h, "Своё место", admin_tg=ADMIN_TG)
        foreign = _make_place(h, "Чужое место", admin_tg=OTHER_ADMIN_TG)
        _set_local_description(
            h,
            place_id=foreign,
            brick_id=NAME_BRICK_ID,
            text="Чужое описание",
            actor_tg=OTHER_ADMIN_TG,
        )
        await _open_system_bricks(h, own)

        await h.callback(f"adm_pbricks:{foreign}", user_id=ADMIN_TG)
        self.assertTrue(_last_alert(h), "чужой список открылся по callback")
        # Экран остался своим: чужое название и чужое описание не показаны.
        self.assertNotIn("Чужое место", h.screen_text())
        self.assertNotIn("Чужое описание", h.screen_text())

        await h.callback(f"adm_pbrick_desc:{NAME_BRICK_ID}", user_id=ADMIN_TG)
        self.assertTrue(_last_alert(h), "ввод открылся без прав")
        await h.text("Подмена", user_id=ADMIN_TG)
        with h.session_factory() as session:
            rows = list(
                session.scalars(
                    select(PlaceBrickDescription).where(
                        PlaceBrickDescription.place_id == own
                    )
                ).all()
            )
        self.assertEqual(rows, [], "текст записался без открытого ввода")

    @scenario_test
    async def test_archived_brick_is_hidden_and_not_editable(self, h: Harness) -> None:
        place_id = _make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        with h.session_factory() as session:
            row = session.get(Brick, NAME_BRICK_ID)
            assert row is not None
            row.status = "archived"
            session.commit()
        await _open_system_bricks(h, place_id)

        self.assertNotIn(
            f"adm_pbrick:{NAME_BRICK_ID}",
            _callbacks(h),
            "архивный кирпичик остался в списке",
        )
        await h.callback(f"adm_pbrick:{NAME_BRICK_ID}", user_id=ADMIN_TG)
        self.assertTrue(_last_alert(h), "архивный кирпичик открылся")

    @scenario_test
    async def test_stale_button_gets_stale_alert(self, h: Harness) -> None:
        place_id = _make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await _open_place_card(h, place_id)
        # Пользователь ушёл в другой раздел: состояние кабинета сброшено.
        await h.callback("main_menu", user_id=ADMIN_TG)
        await h.callback(f"adm_pbrick:{NAME_BRICK_ID}", user_id=ADMIN_TG)
        cat.assert_stale(self, _last_alert(h))
        self.assertFalse(
            _place_description_rows(h), "устаревшая кнопка что-то записала"
        )

    @scenario_test
    async def test_place_sessions_untouched(self, h: Harness) -> None:
        place_id = _make_place(h, "Дом культуры", admin_tg=ADMIN_TG)
        await h.text("/start", user_id=ADMIN_TG)
        with h.session_factory() as session:
            from models import PlaceSession

            before = session.get(PlaceSession, ADMIN_TG)
            before_payload = before.payload if before is not None else None

        await _open_system_bricks(h, place_id)
        await h.callback(f"adm_pbrick:{NAME_BRICK_ID}", user_id=ADMIN_TG)
        await h.callback(f"adm_pbrick_desc:{NAME_BRICK_ID}", user_id=ADMIN_TG)
        await h.text("Текст", user_id=ADMIN_TG)

        with h.session_factory() as session:
            from models import PlaceSession

            after = session.get(PlaceSession, ADMIN_TG)
            after_payload = after.payload if after is not None else None
        self.assertEqual(before_payload, after_payload, "снимок place_sessions изменён")


# --- §3.2: инъекция в конструктор анонса -------------------------------------


@unittest.skipIf(
    HARNESS_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {HARNESS_IMPORT_ERROR}",
)
class WizardLocalDescriptionTests(unittest.TestCase):
    """Пункт 5: мастер показывает локальное описание площадки."""

    @scenario_test
    async def test_question_shows_local_description(self, h: Harness) -> None:
        from tests.test_stage27b_announcement_ui import _seed_place
        from tests.test_stage261_a_wizard_editor import FALLBACK_KEY

        await h.text("/start")
        place_id, _alias = _seed_place(h)
        with h.session_factory() as session:
            set_place_brick_description(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                place_id=place_id,
                brick_id=NAME_BRICK_ID,
                description="Своё пояснение площадки о названии",
            )

        await h.callback("pick_place")
        await h.callback(f"pl_pick:{place_id}")

        screen = h.screen_text()
        self.assertIn("Своё пояснение площадки о названии", screen)
        cat.assert_not_shows(self, screen, FALLBACK_KEY)


# --- store: гонка записи -----------------------------------------------------


class PlaceBrickDescriptionStoreTests(unittest.TestCase):
    """Пункт 9: проигранная гонка не оставляет мусора."""

    def test_stale_update_leaves_single_row(self) -> None:
        from tests.stage23_helpers import TemplateDatabaseTestCase

        case = TemplateDatabaseTestCase()
        case.setUp()
        self.addCleanup(case.doCleanups)
        session = case.session
        owner = case.make_user(ADMIN_TG)
        place = case.make_place("Дом культуры", created_by_user_id=owner.id)
        case.make_place_admin(place, owner)

        created = set_place_brick_description(
            session,
            actor_telegram_user_id=ADMIN_TG,
            place_id=place.id,
            brick_id=NAME_BRICK_ID,
            description="Первое описание",
        )
        self.assertEqual(created.status, "updated")

        # Гонка: экран увидел ревизию, которой в БД уже нет. Чтение строки
        # подменяется, чтобы условный UPDATE не нашёл запись (rowcount != 1).
        real_read = place_brick_store._read_row

        class _StaleRow:
            def __init__(self, row) -> None:
                self.id = row.id
                self.description = row.description
                self.revision = 999

        def stale_read(session_arg, place_id, brick_id):
            row = real_read(session_arg, place_id, brick_id)
            return _StaleRow(row) if row is not None else None

        with mock.patch("place_brick_store._read_row", side_effect=stale_read):
            result = set_place_brick_description(
                session,
                actor_telegram_user_id=ADMIN_TG,
                place_id=place.id,
                brick_id=NAME_BRICK_ID,
                description="Второе описание",
            )

        self.assertEqual(result.status, "stale", result)
        session.expire_all()
        rows = list(
            session.scalars(
                select(PlaceBrickDescription).where(
                    PlaceBrickDescription.place_id == place.id
                )
            ).all()
        )
        self.assertEqual(len(rows), 1, "гонка оставила мусор")
        self.assertEqual(rows[0].description, "Первое описание")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
