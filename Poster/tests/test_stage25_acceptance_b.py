# tests/test_stage25_acceptance_b.py
"""
Poster 2.5, независимая приёмка этапа B (Tester).

Проверяется утверждённое пользовательское поведение (spec §14–§17,
§57 пп. 13–20), а не реализация Developer:

§14 — единая пагинация: 0–5 объектов без стрелок и индикатора; 6+ — пять
объектов на страницу и одна строка `◀️ | 2/4 | ▶️` без пустых кнопок;
индикатор `noop:<page>:<total>` — безопасный no-op (экран и диалог целы);
записи с кнопками удаления («Мои анонсы») тоже пагинируются по пять;
удаление единственной записи последней страницы убирает пагинацию и
приводит страницу к допустимой.

§15 — контекст списков: возврат из карточки и из пикера личных шаблонов
восстанавливает ту же страницу, поисковый запрос и фильтры; после
рестарта процесса используется сохранённый контекст, без stale-alert.

§16 — «📝 Создать анонс» сразу открывает выбор площадки с пагинацией;
`🔎 Найти площадку` | `➕ Новая площадка` одной строкой, затем личный
шаблон, последняя строка — возврат; промежуточного экрана «Как хотите
создать анонс?» нет.

§17 — системный кирпичик `[дата]` всегда отображается как `ДД.ММ (дн)`
с верным днём недели: личный шаблон, стандарт площадки, расширенный
шаблон (дата только в extended) и повторное редактирование доставленного
анонса. Проверяется ВИДИМЫЙ текст готового поста, а не служебный экран
обзора (там внутренний формат `ДД.ММ.ГГГГ` допустим).

Сценарии идут через настоящее `Application.process_update`, FakeBot и
временную SQLite (`tests.integration_harness`). Реальная база, сеть
Telegram и настоящий бот не используются.

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage25_acceptance_b -v
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import select

from models import AnnouncementDraft
from place_state import load_place_session
from place_store import create_place
from template_state import load_template_session
from template_store import get_standard_template
from tests import ui_texts_expect as cat
from tests.integration_harness import (
    DEFAULT_USER_ID,
    HARNESS_IMPORT_ERROR,
    Harness,
    frozen_today,
    scenario_test,
)
from tests.test_stage25_delivery_instances import _stored
from tests.test_stage27b_announcement_ui import (
    _callback,
    _latest_draft,
    _no_photos,
    _open_control,
    _open_edit,
    _seed_personal_template,
    _seed_place,
    _start_to_template,
)
from user_store import get_or_create_user

try:
    INTEGRATION_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_ERROR = str(e)

PREV = "◀️"
NEXT = "▶️"
NOOP_PREFIX = "noop:"
SEARCH_LABEL = "🔎 Найти площадку"
NEW_PLACE_LABEL = "➕ Новая площадка"
TEMPLATE_LABEL = "📝 Создать по личному шаблону"
BACK_LABEL = "◀️ Назад"
STALE_MARK = "больше не работает"

# Даты приёмочного примера владельца (spec §17): 2026-11-02 — понедельник.
DATE_MONDAY = ("09.11.2026", "09.11 (пн)")
DATE_TUESDAY = ("10.11.2026", "10.11 (вт)")
DATE_LATER_TUESDAY = ("17.11.2026", "17.11 (вт)")


# ---------------------------------------------------------------------------
# помощники
# ---------------------------------------------------------------------------


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


def _datas(h: Harness, prefix: str = "") -> list[str]:
    return [
        data
        for _label, data in _flat(h)
        if data is not None and data.startswith(prefix)
    ]


def _labels(h: Harness) -> list[str]:
    return [label for label, _data in _flat(h)]


def _nav_row(h: Harness, *page_prefixes: str) -> list[tuple[str, str | None]]:
    """Строка пагинации: индикатор `noop:` и/или стрелки списка."""
    prefixes = page_prefixes or ("pl_page:", "pl_pickp:", "pl_picksp:", "plm_page:")
    for row in _rows(h):
        if any(
            data is not None
            and (data.startswith(NOOP_PREFIX) or data.startswith(prefixes))
            for _label, data in row
        ):
            return row
    return []


def _alert(h: Harness) -> str:
    if not h.answers():
        return ""
    record = h.answers()[-1]
    args = tuple(record.get("args") or ())
    if args and isinstance(args[0], str):
        return args[0]
    return str(record.get("text") or "")


def _alerts(h: Harness) -> list[str]:
    return h.answer_texts()


def _assert_one_row_per_object(
    test: unittest.TestCase,
    rows: list[list[tuple[str, str | None]]],
    *,
    prefix: str,
    expected: int,
) -> None:
    """Ровно `expected` строк по одному объекту списка, без пустых кнопок."""
    object_rows = [row for row in rows if row and (row[0][1] or "").startswith(prefix)]
    test.assertEqual(len(object_rows), expected, f"объектов на странице: {rows}")
    for row in object_rows:
        test.assertEqual(len(row), 1, f"объект и лишняя кнопка в строке: {row}")
        label, data = row[0]
        test.assertTrue(label.strip(), f"пустая кнопка объекта: {row}")
        test.assertIsNotNone(data, f"кнопка без callback: {row}")


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
        session.commit()
    return place_ids


def _seed_drafts(h: Harness, count: int) -> list[int]:
    """Записи «Мои анонсы» без отправки: строки БД, как после мастера."""
    with h.session_factory() as session:
        user = get_or_create_user(session, DEFAULT_USER_ID, username="u")
        session.flush()
        base = datetime(2026, 11, 1, 12, 0, 0)
        draft_ids = []
        for index in range(count):
            stamp = base + timedelta(minutes=index)
            row = AnnouncementDraft(
                author_user_id=user.id,
                place_id=None,
                place_name_id=None,
                place_display_name=None,
                place_address=None,
                place_url=None,
                place_city=None,
                origin="personal",
                template_id=None,
                template_version=None,
                template_snapshot_id=None,
                stage="questions",
                revision=1,
                created_at=stamp,
                updated_at=stamp,
            )
            session.add(row)
            session.flush()
            draft_ids.append(int(row.id))
        session.commit()
    return draft_ids


def _draft_ids(h: Harness) -> list[int]:
    with h.session_factory() as session:
        rows = session.scalars(
            select(AnnouncementDraft.id).order_by(AnnouncementDraft.id)
        ).all()
    return [int(item) for item in rows]


def superadmin():
    """Супер­администратор только на время блока (живая конфигурация)."""
    return patch("config.SUPERADMIN_USER_IDS", (DEFAULT_USER_ID,))


async def _open_all_places(h: Harness) -> None:
    await h.text("/start")
    await h.callback("places")
    await h.callback("pl_all")
    assert "📚 Все места" in h.screen_text(), h.screen_text()


async def _fill_until_photos(
    h: Harness,
    *,
    date_text: str | None = None,
    title: str = "Анонс",
    date_first: bool = False,
) -> None:
    """Заполняет вопросы до шага фотографий: заголовок, дата, остальное — пропуск."""
    title_done = False
    for _attempt in range(12):
        text = h.screen_text()
        if cat.text("wizard.photos.title") in text:
            return
        first_line = text.splitlines()[0] if text else ""
        is_date_question = "📅 Дата" in first_line
        if is_date_question and date_text:
            await h.text(date_text)
            continue
        if not is_date_question and not title_done and not date_first:
            title_done = True
            await h.text(title)
            continue
        title_done = True
        await h.callback(_callback(h, "aw_qskip"))
    raise AssertionError(f"шаг фотографий не достигнут: {h.screen_text()[:200]!r}")


async def _open_field(h: Harness, title_part: str) -> None:
    """
    Открывает ровно один вопрос по полю доставленного анонса (2.5.3 §5).

    Путь автора: «📝 Текстовые поля» на управлении анонса → пункт списка.
    Отдельного экрана «✏️ Изменить анонс» больше нет.
    """
    await _open_edit(h)
    field = next(
        (
            data
            for label, data in h.live_buttons()
            if data and data.startswith("anf_sel:") and title_part in label
        ),
        None,
    )
    if field is None:
        raise AssertionError(f"нет поля {title_part!r} в списке: {h.live_buttons()}")
    await h.callback(field)


async def _personal_announcement(
    h: Harness,
    template_id: int,
    *,
    date_text: str,
    title: str = "Анонс",
) -> str:
    """Создаёт анонс по личному шаблону и возвращает текст готового поста."""
    await h.callback("pick_place")
    await h.callback("pick_template")
    await h.callback(f"tp_pick:{template_id}")
    await _fill_until_photos(h, date_text=date_text, title=title)
    await _no_photos(h)
    post = h.posts()[-1] or ""
    return post


def _skip_if_unavailable(cls):
    return unittest.skipIf(
        INTEGRATION_ERROR is not None,
        f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
    )(cls)


# ---------------------------------------------------------------------------
# §14: пагинация
# ---------------------------------------------------------------------------


@_skip_if_unavailable
class PaginationAcceptance(unittest.TestCase):
    """П. 1–3: порог 5/6, строка `◀️ | 2/4 | ▶️`, безопасный индикатор."""

    @scenario_test
    async def test_list_of_five_no_pagination(self, h: Harness) -> None:
        _seed_places(h, 5)
        await _open_all_places(h)

        self.assertEqual(
            h.screen_text().count("Зал страниц"), 5, "список из пяти неполон"
        )
        self.assertNotIn(PREV, _labels(h), "при пяти объектах есть левая стрелка")
        self.assertNotIn(NEXT, _labels(h), "при пяти объектах есть правая стрелка")
        self.assertFalse(
            any(data and data.startswith(NOOP_PREFIX) for _l, data in _flat(h)),
            "при пяти объектах есть индикатор страницы",
        )
        self.assertFalse(
            any(data and data.startswith("pl_page:") for _l, data in _flat(h)),
            "при пяти объектах есть навигация",
        )
        self.assertNotIn("Показано", h.screen_text(), "служебный счётчик в списке")

    @scenario_test
    async def test_list_of_six_pages(self, h: Harness) -> None:
        _seed_places(h, 6)
        await _open_all_places(h)

        rows = _rows(h)
        _assert_one_row_per_object(self, rows, prefix="pl_open:", expected=5)
        nav = _nav_row(h, "pl_page:")
        self.assertEqual(
            nav,
            [("1/2", "noop:1:2"), (NEXT, "pl_page:5")],
            f"первая страница не по контракту: {nav}",
        )
        self.assertNotIn(PREV, [label for label, _d in nav], "левая стрелка на первой")
        # Пустых/декоративных кнопок нет вовсе.
        for label, data in _flat(h):
            self.assertTrue(label.strip(), "декоративная пустая кнопка")
            self.assertIsNotNone(data, "кнопка без callback")

        await h.callback("pl_page:5")
        self.assertEqual(
            h.screen_text().count("Зал страниц"), 1, "вторая страница не одна запись"
        )
        self.assertIn("Зал страниц 6", h.screen_text())
        nav = _nav_row(h, "pl_page:")
        self.assertEqual(
            nav,
            [(PREV, "pl_page:0"), ("2/2", "noop:2:2")],
            f"последняя страница не по контракту: {nav}",
        )
        self.assertNotIn(
            NEXT, [label for label, _d in nav], "правая стрелка на последней"
        )

    @scenario_test
    async def test_indicator_is_noop(self, h: Harness) -> None:
        _seed_drafts(h, 6)
        await h.text("/start")
        await h.callback("my_announcements")
        self.assertIn(("1/2", "noop:1:2"), _flat(h))

        text_before = h.screen_text()
        buttons_before = h.screen_buttons()
        screens_before = len(h.screens())
        edits_before = len(h.edits())
        answers_before = len(h.answers())
        drafts_before = _draft_ids(h)

        await h.callback("noop:1:2")

        self.assertEqual(len(h.screens()), screens_before, "индикатор создал сообщение")
        self.assertEqual(len(h.edits()), edits_before, "индикатор перерисовал экран")
        self.assertEqual(
            len(h.answers()), answers_before + 1, "спиннер не закрыт ответом"
        )
        self.assertEqual(_alerts(h), [], "индикатор показал alert")
        self.assertEqual(h.screen_text(), text_before, "экран изменился")
        self.assertEqual(h.screen_buttons(), buttons_before, "клавиатура изменилась")
        self.assertEqual(_draft_ids(h), drafts_before, "изменились данные")
        self.assertTrue(text_before and buttons_before)


@_skip_if_unavailable
class AnnouncementRecordsPaginationAcceptance(unittest.TestCase):
    """П. 4–6: «Мои анонсы» — записи с удалением и та же схема страниц."""

    @scenario_test
    async def test_records_with_delete_buttons(self, h: Harness) -> None:
        _seed_drafts(h, 6)
        await h.text("/start")
        await h.callback("my_announcements")

        rows = _rows(h)
        record_rows = [
            row for row in rows if row and (row[0][1] or "").startswith("aw_open:")
        ]
        self.assertEqual(len(record_rows), 5, f"записей на странице: {rows}")
        for row in record_rows:
            self.assertEqual(len(row), 2, f"запись не «Название | 🗑»: {row}")
            self.assertTrue((row[0][1] or "").startswith("aw_open:"))
            self.assertEqual(row[1][0], "🗑", f"нет кнопки удаления: {row}")
            self.assertTrue((row[1][1] or "").startswith("aw_del:"))
        self.assertEqual(len(_datas(h, "aw_open:")), 5, "показано не пять записей")
        self.assertEqual(_nav_row(h, "aw_home_page:")[0], ("1/2", "noop:1:2"))

    @scenario_test
    async def test_delete_last_item_last_page(self, h: Harness) -> None:
        draft_ids = _seed_drafts(h, 6)
        await h.text("/start")
        await h.callback("my_announcements")
        await h.callback("aw_home_page:1")

        only = _datas(h, "aw_open:")
        self.assertEqual(len(only), 1, f"на последней странице не одна запись: {only}")
        draft_id = int(only[0].split(":")[1])
        self.assertEqual(draft_id, draft_ids[0], "удаляется не последняя запись списка")

        await h.callback(f"aw_del:{draft_id}")
        await h.callback(f"aw_del_yes:{draft_id}")

        self.assertIn("удал", _alert(h).lower())
        self.assertNotIn(draft_id, _draft_ids(h), "запись не удалена из базы")
        self.assertEqual(len(_datas(h, "aw_open:")), 5, "показано не пять записей")
        self.assertFalse(
            any(data and data.startswith(NOOP_PREFIX) for _l, data in _flat(h)),
            "индикатор остался после исчезновения второй страницы",
        )
        self.assertEqual(
            _datas(h, "aw_home_page:"), [], "стрелки остались при пяти записях"
        )
        ui = h.user_data().get("announcement_ui") or {}
        self.assertEqual(ui.get("home_page"), 0, "страница не приведена к допустимой")

        # Старый callback на исчезнувшую страницу показывает новую последнюю,
        # а не пустой экран.
        await h.callback("aw_home_page:1")
        self.assertEqual(len(_datas(h, "aw_open:")), 5, "пустая страница после clamp")
        self.assertNotIn("На этой странице анонсов нет", h.screen_text())

    @scenario_test
    async def test_my_announcements_pagination(self, h: Harness) -> None:
        _seed_drafts(h, 11)
        await h.text("/start")
        await h.callback("my_announcements")

        self.assertEqual(
            _nav_row(h, "aw_home_page:"),
            [("1/3", "noop:1:3"), (NEXT, "aw_home_page:1")],
            "первая страница трёхстраничного списка",
        )
        self.assertEqual(len(_datas(h, "aw_open:")), 5)

        await h.callback("aw_home_page:1")
        self.assertEqual(
            _nav_row(h, "aw_home_page:"),
            [(PREV, "aw_home_page:0"), ("2/3", "noop:2:3"), (NEXT, "aw_home_page:2")],
            "средняя страница списка",
        )
        self.assertEqual(len(_datas(h, "aw_open:")), 5)

        await h.callback("aw_home_page:2")
        self.assertEqual(
            _nav_row(h, "aw_home_page:"),
            [(PREV, "aw_home_page:1"), ("3/3", "noop:3:3")],
            "последняя страница списка",
        )
        self.assertEqual(len(_datas(h, "aw_open:")), 1)

        await h.callback("aw_home_page:0")
        self.assertEqual(
            _nav_row(h, "aw_home_page:"),
            [("1/3", "noop:1:3"), (NEXT, "aw_home_page:1")],
            "возврат на первую страницу",
        )


# ---------------------------------------------------------------------------
# §15: контекст списков
# ---------------------------------------------------------------------------


@_skip_if_unavailable
class ListContextAcceptance(unittest.TestCase):
    """П. 7–10: страница/поиск переживают карточку и рестарт."""

    @scenario_test
    async def test_manage_back_keeps_page(self, h: Harness) -> None:
        _seed_places(h, 12)
        with superadmin():
            await h.text("/start")
            await h.callback("manage_places")
            await h.callback("plm_page:10")
            self.assertEqual(
                _nav_row(h, "plm_page:"),
                [(PREV, "plm_page:5"), ("3/3", "noop:3:3")],
                "третья страница справочника не открылась",
            )
            cards = _datas(h, "plm_open:")
            self.assertTrue(cards, "на третьей странице нет площадок")
            await h.callback(cards[-1])
            self.assertIn("🛡 Место", h.screen_text())

            await h.callback("adm_places")

        state = load_place_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertEqual(state["phase"], "manage_list")
        self.assertEqual(state.get("page"), 10, "страница списка не восстановлена")
        self.assertEqual(
            h.screen_text().count("Зал страниц"), 2, "показана не третья страница"
        )
        self.assertIn("12. Зал страниц 12", h.screen_text())
        self.assertEqual(
            _nav_row(h, "plm_page:"),
            [(PREV, "plm_page:5"), ("3/3", "noop:3:3")],
            "навигация после возврата не та",
        )

    @scenario_test
    async def test_manage_back_keeps_search(self, h: Harness) -> None:
        _seed_places(h, 12)
        with superadmin():
            await h.text("/start")
            await h.callback("manage_places")
            await h.callback("plm_search")
            await h.text("Зал")
            await h.callback("plm_spage:5")
            cards = _datas(h, "plm_open:")
            self.assertTrue(cards, "на второй странице поиска нет площадок")
            await h.callback(cards[-1])
            self.assertIn("🛡 Место", h.screen_text())

            await h.callback("adm_places")

        state = load_place_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertEqual(state["phase"], "manage_search_results")
        self.assertEqual(state.get("query"), "Зал", "запрос поиска потерян")
        self.assertEqual(state.get("page"), 5, "страница поиска не восстановлена")
        self.assertIn("Результаты поиска: «Зал»", h.screen_text())
        self.assertIn(
            "6. Зал страниц 3", h.screen_text(), "показана не вторая страница"
        )
        self.assertNotIn("1. Зал страниц 1", h.screen_text())
        self.assertEqual(
            _nav_row(h, "plm_spage:"),
            [(PREV, "plm_spage:0"), ("2/3", "noop:2:3"), (NEXT, "plm_spage:10")],
            "навигация результатов поиска после возврата",
        )

    @scenario_test
    async def test_tp_pick_back_keeps_context(self, h: Harness) -> None:
        _seed_places(h, 12)
        _seed_personal_template(h)
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback("pl_pick_search")
        await h.text("Зал")
        await h.callback("pl_picksp:5")
        self.assertIn("6. Зал страниц 3", h.screen_text())
        self.assertNotIn("1. Зал страниц 1", h.screen_text())

        await h.callback("pick_template")
        self.assertIn((BACK_LABEL, "tp_pick_back"), _flat(h))
        answers_before = len(_alerts(h))

        await h.callback("tp_pick_back")

        self.assertEqual(_alerts(h)[answers_before:], [], "возврат дал stale-alert")
        state = load_place_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertEqual(state["phase"], "pick_search_results")
        self.assertEqual(state.get("query"), "Зал", "запрос пикера потерян")
        self.assertEqual(state.get("page"), 5, "страница поиска пикера потеряна")
        cat.assert_shows(
            self,
            h.screen_text(),
            "places.pick_search.ploschadki_zaprosu",
            query="Зал",
        )
        self.assertEqual(
            _nav_row(h, "pl_picksp:"),
            [(PREV, "pl_picksp:0"), ("2/3", "noop:2:3"), (NEXT, "pl_picksp:10")],
            "навигация пикера после возврата",
        )
        self.assertIsNone(
            load_template_session(DEFAULT_USER_ID), "сессия шаблонов не закрыта"
        )

    @scenario_test
    async def test_restart_back_no_stale(self, h: Harness) -> None:
        _seed_places(h, 7)
        with superadmin():
            await h.text("/start")
            await h.callback("manage_places")
            await h.callback("plm_page:5")
            cards = _datas(h, "plm_open:")
            self.assertTrue(cards)
            await h.callback(cards[-1])
            self.assertIn("🛡 Место", h.screen_text())

            await h.restart()

            answers_before = len(_alerts(h))
            await h.callback("adm_places")
            self.assertEqual(
                _alerts(h)[answers_before:], [], "stale-alert после рестарта"
            )

        cat.assert_not_stale(self, _alert(h))
        state = load_place_session(DEFAULT_USER_ID)
        assert state is not None
        self.assertEqual(state["phase"], "manage_list")
        self.assertEqual(state.get("page"), 5, "страница потеряна после рестарта")
        self.assertIn("7. Зал страниц 7", h.screen_text())


# ---------------------------------------------------------------------------
# §16: выбор площадки при создании анонса
# ---------------------------------------------------------------------------


@_skip_if_unavailable
class PickerLayoutAcceptance(unittest.TestCase):
    """П. 11: сразу пикер с пагинацией, строки `поиск|новая`, шаблон, назад."""

    @scenario_test
    async def test_picker_layout(self, h: Harness) -> None:
        _seed_places(h, 7)
        _seed_personal_template(h)
        await h.text("/start")
        await h.callback("pick_place")

        text = h.screen_text()
        self.assertIn("📝 Создать анонс", text, "открылся не выбор площадки")
        self.assertNotIn("Как хотите создать анонс", text, "промежуточный экран")
        self.assertNotIn(
            "Как хотите создать анонс", " ".join(_labels(h)), "промежуточный экран"
        )

        rows = _rows(h)
        _assert_one_row_per_object(self, rows, prefix="pl_pick:", expected=5)
        nav = _nav_row(h, "pl_pickp:")
        self.assertEqual(
            nav, [("1/2", "noop:1:2"), (NEXT, "pl_pickp:5")], f"пагинация пикера: {nav}"
        )
        search_row: list[tuple[str, str | None]] = [
            (SEARCH_LABEL, "pl_pick_search"),
            (NEW_PLACE_LABEL, "pl_pick_add"),
        ]
        self.assertIn(search_row, rows, f"поиск и создание не одной строкой: {rows}")
        self.assertEqual(rows.count(search_row), 1)
        template_row: list[tuple[str, str | None]] = [(TEMPLATE_LABEL, "pick_template")]
        self.assertEqual(rows.count(template_row), 1, "нет строки личного шаблона")
        self.assertGreater(
            rows.index(template_row),
            rows.index(search_row),
            "личный шаблон выше строки поиска",
        )
        self.assertEqual(
            rows[-1],
            [(BACK_LABEL, "main_menu")],
            f"последняя строка не возврат: {rows}",
        )
        self.assertEqual(
            len(rows),
            5 + 1 + 1 + 1 + 1,
            f"лишние строки в пикере (5 мест + навигация + поиск/новая + "
            f"шаблон + назад): {rows}",
        )

    @scenario_test
    async def test_picker_is_first_screen_and_has_back(self, h: Harness) -> None:
        _seed_places(h, 2)
        await h.text("/start")
        await h.callback("pick_place")
        # Никакого дополнительного шага: пикер — первый экран создания.
        self.assertFalse(
            _datas(h, "aw_start"), "появился старый промежуточный вход мастера"
        )
        self.assertIn(("◀️ Назад", "main_menu"), _flat(h))

        await h.callback("main_menu")
        self.assertIn(("📝 Создать анонс", "pick_place"), _flat(h))


# ---------------------------------------------------------------------------
# §17: формат системного кирпичика `[дата]`
# ---------------------------------------------------------------------------


@_skip_if_unavailable
class DateFormatAcceptance(unittest.TestCase):
    """П. 12–14: `ДД.ММ (дн)` во всех ветках рендера готового анонса."""

    @scenario_test
    async def test_date_format_basic(self, h: Harness) -> None:
        await h.text("/start")
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="С датой",
            specs=[("main", "Основное", "short_text", {})],
            content="[main]\n[дата]",
        )
        with frozen_today(2026, 11, 2):
            for raw, formatted in (DATE_MONDAY, DATE_TUESDAY, DATE_LATER_TUESDAY):
                post = await _personal_announcement(
                    h, template_id, date_text=raw, title="Концерт"
                )
                self.assertIn(formatted, post, f"нет {formatted!r} в посте: {post!r}")
                self.assertNotIn(raw, post, f"в посте остался {raw!r}: {post!r}")
                await h.callback("main_menu")

    @scenario_test
    async def test_date_format_all_branches(self, h: Harness) -> None:
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        with h.session_factory() as session:
            standard_id = get_standard_template(
                session, DEFAULT_USER_ID, place_id
            ).template_id
        extended_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Расширенный",
            specs=[("main", "Основное", "short_text", {})],
            content="[main]",
            extended_content="[main]\n[дата]",
        )
        raw, formatted = DATE_MONDAY
        raw_next, formatted_next = DATE_TUESDAY
        raw_edit, formatted_edit = DATE_LATER_TUESDAY

        with frozen_today(2026, 11, 2):
            # 1) Стандартный шаблон площадки (встроенный текст с [дата]).
            await _start_to_template(h, place_id)
            await h.callback(_callback(h, "aw_template", f":{standard_id}"))
            await _fill_until_photos(h, date_text=raw, title="Стандарт")
            await _no_photos(h)
            standard_post = h.posts()[-1] or ""
            self.assertIn(formatted, standard_post, standard_post)
            self.assertNotIn(raw, standard_post, standard_post)
            standard_draft_id = _latest_draft(h).id

            # 2) Расширенный шаблон: [дата] только в extended-тексте.
            await h.callback("main_menu")
            await h.callback("pick_place")
            await h.callback("pick_template")
            await h.callback(f"tp_pick:{extended_id}")
            await _fill_until_photos(h, date_text=None, title="Главное")
            await _no_photos(h)
            self.assertNotIn(formatted, h.posts()[-1] or "", "дата до выбора доп. поля")

            await _open_edit(h)
            await _open_control(h, "extras")
            await h.callback(_callback(h, "aw_exedit"))
            self.assertIn("📅 Дата", h.screen_text())
            await h.text(raw_next)
            extended_post = h.posts()[-1] or ""
            self.assertIn(formatted_next, extended_post, extended_post)
            self.assertNotIn(raw_next, extended_post, extended_post)

            # 3) Повторное редактирование доставленного стандартного анонса.
            await h.callback("main_menu")
            await h.callback("my_announcements")
            await h.callback(f"aw_open:{standard_draft_id}")
            # 2.5.3 §5: правка поля доставленного анонса — «📝 Текстовые поля»
            # на самом управлении, затем пункт списка (без временного экрана).
            await _open_field(h, "Дата")
            await h.text(raw_edit)
            # §28 (2.5): правка доставленного идёт НА МЕСТЕ — анонс без фото
            # доводится `editMessageText`, поэтому видимый текст берётся из
            # фактического содержимого сообщения, а не из записи `send_message`.
            edited_ids = _stored(h, standard_draft_id).delivered_message_ids
            self.assertTrue(edited_ids, "адреса доставки потеряны")
            edited_post = h.bot.content_text(h.chat_id, edited_ids[0]) or ""
            self.assertIn(formatted_edit, edited_post, edited_post)
            self.assertNotIn(raw_edit, edited_post, edited_post)
            self.assertNotIn(formatted, edited_post, "старая дата осталась в посте")

    @scenario_test
    async def test_reopen_from_my_announcements(self, h: Harness) -> None:
        await h.text("/start")
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="С датой",
            specs=[("main", "Основное", "short_text", {})],
            content="[main]\n[дата]",
        )
        raw, formatted = DATE_MONDAY
        with frozen_today(2026, 11, 2):
            post = await _personal_announcement(
                h, template_id, date_text=raw, title="Концерт"
            )
            self.assertIn(formatted, post)
            draft_id = _latest_draft(h).id
            posts_before = list(h.posts())

            await h.callback("main_menu")
            await h.callback("my_announcements")
            await h.callback(f"aw_open:{draft_id}")

            # §29 (новая копия при открытии) — этап C; на этапе B достаточно,
            # чтобы ВИДИМЫЙ текст доставленного/нового поста был `ДД.ММ (дн)`.
            posts_after = list(h.posts())
            checked = posts_after[len(posts_before) :] or posts_after[-1:]
            self.assertTrue(checked, "после открытия нет видимого поста")
            for text in checked:
                self.assertIsNotNone(text)
                self.assertIn(formatted, text or "", text)
                self.assertNotIn(raw, text or "", f"в посте остался {raw!r}: {text!r}")
            self.assertEqual(
                posts_after[: len(posts_before)],
                posts_before,
                "старые исторические экземпляры переписаны",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
