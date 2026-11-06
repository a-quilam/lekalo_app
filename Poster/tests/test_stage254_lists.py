# tests/test_stage254_lists.py
"""
Poster 2.5.4 §4 + §6 — независимые тесты Tester (тест-до-кода).

Проверяется пользовательский контракт, а не реализация:

§4. Клавиатура меню площадок:
   - в хабе «📍 Места» кнопки `🔎 Найти место` и `➕ Добавить место` стоят
     ОДНОЙ строкой; ниже — `📚 Все места` и `◀️ Назад`;
   - та же компоновка у списка площадок при создании анонса;
   - навигация не сломана (поиск, список, возврат).

§6. «Мои анонсы» — понятные названия:
   - подпись кнопки записи: `Заголовок — Место (состояние)`, где заголовок —
     название мероприятия, а не площадка;
   - состояние — только `готов` (`publication_status=sent`) или `черновик`
     (все остальные статусы, включая технические);
   - пустой заголовок — `Без названия`; анонс по личному шаблону без
     площадки показывает имя личного шаблона;
   - нумерация и эмодзи «▶️» в кнопках списка отсутствуют; номер остаётся
     только в тексте экрана;
   - механика не изменилась: корзина 🗑 с подтверждением, пагинация
     5/страницу с возвратом на страницу, лимиты 4+4.

Сценарии идут через настоящее приложение (`bot.build_application`), FakeBot и
временную SQLite (`tests/integration_harness`). Реальная база, сеть Telegram
и настоящий бот не используются.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage254_lists -v
"""

from __future__ import annotations

import unittest

try:
    from announcement_store import (
        choose_announcement_template,
        confirm_announcement_place,
        save_announcement_value,
        start_announcement_wizard,
    )
    from announcement_wizard import snapshot_brick_ids_by_code
    from models import AnnouncementDraft, utcnow
    from template_store import get_standard_template
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        Harness,
        scenario_test,
    )
    from tests.test_poster2_24_p3_announcements_tester import (
        _seed_draft_rows,
        _seed_sent,
    )
    from tests.test_stage27b_announcement_ui import (
        _seed_personal_template,
        _seed_place,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


LIST_HEAD = "📂 Мои анонсы"
TITLE_CODE = "название"
SEARCH_PLACE = "🔎 Найти место"
ADD_PLACE = "➕ Добавить место"
ALL_PLACES = "📚 Все места"
PICK_SEARCH = "🔎 Найти площадку"
PICK_ADD = "➕ Новая площадка"
BACK = "◀️ Назад"


# --- наблюдение за экраном ---------------------------------------------------


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


def _data(h: Harness, prefix: str) -> list[str]:
    return [data for _label, data in _flat(h) if data and data.startswith(prefix)]


def _row_with(h: Harness, data: str) -> list[tuple[str, str | None]] | None:
    for row in _rows(h):
        if any(cell[1] == data for cell in row):
            return row
    return None


async def _open_list(h: Harness) -> None:
    await h.callback("main_menu")
    await h.callback("my_announcements")
    if LIST_HEAD not in h.screen_text():
        raise AssertionError(f"не открылся список анонсов: {h.screen_text()[:300]!r}")


# --- подготовка данных --------------------------------------------------------


def _ensure_user(h: Harness) -> None:
    """Пользователь Telegram регистрируется при первом контакте; сев — до него."""
    from user_store import get_or_create_user

    with h.session_factory() as session:
        get_or_create_user(session, DEFAULT_USER_ID, username="lists-owner")
        session.commit()


def _place_with_title(h: Harness, *, place_name: str, title: str | None) -> int:
    """Черновик по площадке: стандарт подтверждён, заголовок задан/пуст."""
    _ensure_user(h)
    place_id, _alias = _seed_place(h, name=place_name)
    with h.session_factory() as session:
        draft = start_announcement_wizard(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            place_id=place_id,
        )
        confirmed = confirm_announcement_place(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft.draft_id,
            expected_revision=draft.revision,
        )
        standard = get_standard_template(session, DEFAULT_USER_ID, place_id)
        chosen = choose_announcement_template(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=confirmed.draft_id,
            template_id=standard.template_id,
            expected_revision=confirmed.revision,
        )
        if title is not None:
            assert chosen.template_snapshot is not None
            brick_id = snapshot_brick_ids_by_code(chosen.template_snapshot)[TITLE_CODE]
            chosen = save_announcement_value(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=chosen.draft_id,
                brick_id=brick_id,
                value=title,
                expected_revision=chosen.revision,
            )
        return chosen.draft_id


def _personal_with_title(h: Harness, *, title: str | None) -> int:
    """Черновик по личному шаблону: площадки нет, заголовок задан/пуст."""
    template_id, _revision, _bricks = _seed_personal_template(
        h,
        name="Личный «Вечер»",
        specs=[("f0", "Поле", "short_text", {})],
        content="[название]\n[f0]",
    )
    with h.session_factory() as session:
        draft = start_announcement_wizard(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            template_id=template_id,
            origin="personal",
        )
        if title is not None:
            assert draft.template_snapshot is not None
            brick_id = snapshot_brick_ids_by_code(draft.template_snapshot)[TITLE_CODE]
            draft = save_announcement_value(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.draft_id,
                brick_id=brick_id,
                value=title,
                expected_revision=draft.revision,
            )
        return draft.draft_id


def _mark_sent(h: Harness, draft_id: int) -> None:
    """
    Подготовка данных: запись «как после доставки».

    Прямой UPDATE строки БД, потому что настоящая доставка требует полного
    прохождения вопросов и шага фото — для подписи списка это не нужно.
    Схема принимает переход только с замороженным payload (§P3).
    """
    with h.session_factory() as session:
        row = session.get(AnnouncementDraft, draft_id)
        if row is None:
            raise AssertionError(f"черновик {draft_id} не найден")
        now = utcnow()
        row.publication_status = "sent"
        row.publication_text = "Готовый анонс"
        row.publication_photos = "[]"
        row.publication_started_at = now
        row.published_at = now
        row.stage = "completed"
        session.commit()


def _label_by_place(h: Harness, place: str) -> str:
    for label, data in h.screen_buttons():
        if data and data.startswith("aw_open:") and place in label:
            return label
    raise AssertionError(
        f"нет записи «{place}» в списке: {h.screen_buttons()!r}; "
        f"экран: {h.screen_text()[:400]!r}"
    )


# --- §6. Формат подписи ------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class EntryLabelFormatTests(unittest.TestCase):
    """§6: `Заголовок — Место (состояние)` без номера и эмодзи."""

    @scenario_test
    async def test_sent_entry_shows_title_place_and_gotov(self, h) -> None:
        draft_id = _place_with_title(h, place_name="РанДом", title="Концерт")
        _mark_sent(h, draft_id)
        await h.text("/start")
        await _open_list(h)

        self.assertEqual(
            _label_by_place(h, "РанДом"),
            "Концерт — РанДом (готов)",
            "подпись готового анонса: заголовок — место (состояние)",
        )
        self.assertIn(
            "1. Концерт — РанДом (готов)",
            h.screen_text(),
            "строка текста экрана: номер сохраняется, название — новое",
        )

    @scenario_test
    async def test_draft_entry_shows_chernovik(self, h) -> None:
        _place_with_title(h, place_name="Красный Маяк", title="Киноклуб")
        await h.text("/start")
        await _open_list(h)

        self.assertEqual(
            _label_by_place(h, "Красный Маяк"),
            "Киноклуб — Красный Маяк (черновик)",
        )
        self.assertIn("1. Киноклуб — Красный Маяк (черновик)", h.screen_text())

    @scenario_test
    async def test_missing_title_shows_bez_nazvaniya(self, h) -> None:
        draft_id = _place_with_title(h, place_name="РанДом", title=None)
        _mark_sent(h, draft_id)
        await h.text("/start")
        await _open_list(h)

        self.assertEqual(
            _label_by_place(h, "РанДом"),
            "Без названия — РанДом (готов)",
            "пустой заголовок — понятная заглушка из каталога",
        )

    @scenario_test
    async def test_personal_template_without_place_shows_template_name(self, h) -> None:
        draft_id = _personal_with_title(h, title="Лекция")
        _mark_sent(h, draft_id)
        await h.text("/start")
        await _open_list(h)

        self.assertEqual(
            _label_by_place(h, "Личный «Вечер»"),
            "Лекция — Личный «Вечер» (готов)",
            "у личного анонса места нет — показывается имя личного шаблона",
        )

    @scenario_test
    async def test_technical_status_is_just_chernovik(self, h) -> None:
        """`sending`/`partial`/`failed` — всё это «черновик», без тех. слов."""
        draft_id = _place_with_title(h, place_name="РанДом", title="Концерт")
        with h.session_factory() as session:
            row = session.get(AnnouncementDraft, draft_id)
            now = utcnow()
            row.publication_status = "failed"
            row.publication_text = "Текст"
            row.publication_photos = "[]"
            row.publication_started_at = now
            session.commit()
        await h.text("/start")
        await _open_list(h)

        label = _label_by_place(h, "РанДом")
        self.assertEqual(label, "Концерт — РанДом (черновик)")
        for technical in (
            "ошибка",
            "отправка",
            "частично",
            "sending",
            "partial",
            "failed",
        ):
            self.assertNotIn(technical, label.lower(), f"технический статус: {label}")

    @scenario_test
    async def test_buttons_have_no_number_or_play_emoji(self, h) -> None:
        _place_with_title(h, place_name="РанДом", title="Концерт")
        _place_with_title(h, place_name="Красный Маяк", title="Киноклуб")
        await h.text("/start")
        await _open_list(h)

        labels = [label for label, data in h.screen_buttons() if data]
        opens = [label for label, data in h.screen_buttons() if data]
        self.assertTrue(opens, "список записей пуст")
        for label in labels:
            self.assertNotIn("▶️", label, f"эмодзи нумерации в кнопке: {label!r}")
            self.assertNotRegex(
                label, r"^\s*\d+\.", f"номер в кнопке списка: {label!r}"
            )

    @scenario_test
    async def test_long_titles_keep_place_and_state(self, h) -> None:
        """§6.3: длинный заголовок сокращается, место и состояние видны."""
        long_title = "Очень длинный заголовок мероприятия " * 4
        _place_with_title(h, place_name="Красный Маяк", title=long_title)
        await h.text("/start")
        await _open_list(h)

        label = _label_by_place(h, "Красный Маяк")
        self.assertIn("Красный Маяк", label)
        self.assertTrue(label.endswith("(черновик)"), label)
        self.assertIn("…", label, "длинный заголовок должен быть сокращён")
        self.assertLessEqual(len(label), 90, f"подпись слишком длинная: {label!r}")


# --- §6. Механика не изменилась ----------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ListMechanicsTests(unittest.TestCase):
    """Механика списка: корзина, подтверждение, страницы, лимиты."""

    @scenario_test
    async def test_trash_pair_and_delete_confirmation_work(self, h) -> None:
        draft_id = _place_with_title(h, place_name="РанДом", title="Концерт")
        await h.text("/start")
        await _open_list(h)

        opens = [data for data in _data(h, "aw_open:")]
        self.assertTrue(opens)
        row = _row_with(h, opens[0])
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(len(row), 2, f"запись не «название | 🗑»: {row}")
        self.assertEqual(row[1][0], "🗑", f"нет кнопки удаления: {row}")

        await h.callback(f"aw_del:{draft_id}")
        self.assertIn("Удалить анонс", h.screen_text())
        self.assertIn(
            "РанДом",
            h.screen_text(),
            "подтверждение удаления — существующий маршрут, не менялся",
        )
        await h.callback(f"aw_del_no:{draft_id}")
        self.assertIn("Концерт — РанДом (черновик)", h.screen_text())
        with h.session_factory() as session:
            self.assertIsNotNone(session.get(AnnouncementDraft, draft_id))

        await h.callback(f"aw_del:{draft_id}")
        await h.callback(f"aw_del_yes:{draft_id}")
        with h.session_factory() as session:
            self.assertIsNone(
                session.get(AnnouncementDraft, draft_id), "запись не удалена"
            )
        self.assertIn(LIST_HEAD, h.screen_text())

    @scenario_test
    async def test_pagination_five_per_page_and_return_to_page(self, h) -> None:
        _ensure_user(h)
        _seed_draft_rows(h, count=4, start_at=utcnow())
        for _index in range(4):
            _seed_sent(h, utcnow())
        await h.text("/start")
        await _open_list(h)

        self.assertEqual(len(_data(h, "aw_open:")), 5, "пять записей на странице")
        nav = [
            row
            for row in _rows(h)
            if any((d or "").startswith("noop:") for _l, d in row)
        ]
        self.assertTrue(nav, "нет строки пагинации")
        next_page = [d for _l, d in nav[0] if d and d.startswith("aw_home_page:")]
        self.assertTrue(next_page, f"нет правой стрелки: {nav}")
        await h.callback(next_page[0])
        self.assertEqual(len(_data(h, "aw_open:")), 3, "на второй странице три записи")

        # Возврат на страницу: подтверждение удаления не сбрасывает страницу.
        victim = _data(h, "aw_open:")[0].split(":")[1]
        await h.callback(f"aw_del:{victim}")
        await h.callback(f"aw_del_no:{victim}")
        self.assertEqual(
            len(_data(h, "aw_open:")),
            3,
            "возврат из подтверждения обязан сохранить ту же страницу",
        )
        self.assertIn("2/2", [label for label, _d in h.screen_buttons()])

    @scenario_test
    async def test_draft_limit_four_still_evicts_oldest(self, h) -> None:
        template_id, _revision, _bricks = _seed_personal_template(
            h, name="Шаблон лимита", specs=[("f0", "Поле", "short_text", {})]
        )
        ids: list[int] = []
        for _ in range(5):
            with h.session_factory() as session:
                draft = start_announcement_wizard(
                    session,
                    actor_telegram_user_id=DEFAULT_USER_ID,
                    template_id=template_id,
                    origin="personal",
                )
                ids.append(draft.draft_id)
        with h.session_factory() as session:
            rows = [
                row.id
                for row in session.query(AnnouncementDraft)
                .order_by(AnnouncementDraft.id)
                .all()
            ]
        self.assertEqual(len(rows), 4, "лимит черновиков 4 не изменился")
        self.assertNotIn(ids[0], rows, "вытесняется самый старый черновик")


# --- §4. Клавиатура площадок -------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlacesKeyboardTests(unittest.TestCase):
    """§4: поиск и добавление места — одной строкой, навигация цела."""

    @scenario_test
    async def test_hub_search_and_add_share_one_row(self, h) -> None:
        await h.text("/start")
        await h.callback("places")
        self.assertIn("📍 Места", h.screen_text())

        row = _row_with(h, "pl_search")
        self.assertIsNotNone(row, f"нет кнопки поиска: {h.screen_buttons()!r}")
        assert row is not None
        self.assertEqual(
            row,
            [(SEARCH_PLACE, "pl_search"), (ADD_PLACE, "pl_add")],
            "поиск и добавление — одна строка в порядке владельца",
        )
        self.assertEqual(_row_with(h, "pl_all"), [(ALL_PLACES, "pl_all")])
        self.assertEqual(_row_with(h, "pl_search"), row)
        back_row = [r for r in _rows(h) if ("◀️ Назад", "main_menu") in r]
        self.assertEqual(back_row, [[(BACK, "main_menu")]])

        # Навигация не сломана: поиск открывает ввод, «назад» возвращает хаб.
        await h.callback("pl_search")
        self.assertIn("Введите название или", h.screen_text())
        await h.callback("main_menu")
        await h.callback("places")
        await h.callback("pl_all")
        self.assertIn("пока нет мест", h.screen_text())
        self.assertIn((ADD_PLACE, "pl_add"), h.screen_buttons())
        await h.callback("main_menu")
        self.assertIn("Что хотите сделать?", h.screen_text())

    @scenario_test
    async def test_announcement_place_picker_keeps_one_row(self, h) -> None:
        """Список площадок при создании анонса — те же действия одной строкой."""
        await h.text("/start")
        await h.callback("pick_place")
        row = _row_with(h, "pl_pick_search")
        self.assertIsNotNone(row, f"нет поиска площадки: {h.screen_buttons()!r}")
        assert row is not None
        self.assertEqual(
            row,
            [(PICK_SEARCH, "pl_pick_search"), (PICK_ADD, "pl_pick_add")],
            "создание анонса: поиск и новая площадка — одна строка",
        )
        # Навигация не сломана.
        await h.callback("pl_pick_search")
        self.assertIn("Введите название или", h.screen_text())
        await h.callback("main_menu")
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback("main_menu")
        self.assertIn("Что хотите сделать?", h.screen_text())

    @scenario_test
    async def test_wizard_place_list_keeps_its_own_actions(self, h) -> None:
        """
        У списка площадок конструктора действий «добавить место» нет: там
        только поиск, «все места» и возврат. Компоновка §4 касается хаба и
        выбора площадки при создании анонса, а не шага 1 мастера.
        """
        await h.text("/start")
        await h.callback("aw_start")
        await h.callback("aw_new")
        self.assertIn("Шаг 1. Выберите место", h.screen_text())
        search_row = _row_with(h, "aw_place_search")
        self.assertIsNotNone(search_row)
        assert search_row is not None
        self.assertEqual(search_row, [(SEARCH_PLACE, "aw_place_search")])
        self.assertEqual(_data(h, "aw_place_all:"), ["aw_place_all:0"])
        self.assertEqual(_data(h, "pl_add"), [], "добавление места не появилось")
        await h.callback("aw_place_all:0")
        self.assertIn("В справочнике пока нет мест", h.screen_text())
        await h.callback("aw_place_search")
        self.assertIn("Поиск места", h.screen_text())
        await h.callback("aw_place_back")
        self.assertIn("Шаг 1. Выберите место", h.screen_text())
