# tests/test_poster2_u2b_finish.py
"""
Poster 2.0 U2b, финал доставки и компактная правка готового анонса.

Poster 2.5 (C2b-2): экрана «✅ Анонс готов» больше нет (spec §18). После
доставки конструктор остаётся на рабочем экране редактора «✏️ Изменить
анонс» (`wizard.finished.edit_text`), а результат уходит ответом на callback.

Проверяется:

- после доставки на экране компактный редактор, а не итоговый экран;
- редактор открывает категории правки и не отправляет копию;
- «В меню» оставляет готовый пост и ровно один служебный экран;
- инвариант порядка: все `message_id` постов ниже `message_id` служебного
  экрана — по фактическим id из FakeBot (`call_message_ids`), в том числе
  после замены версии;
- H3: изменения применяются сразу, строки «есть неприменённые изменения» и
  кнопки «Применить изменения» больше нет; повторное сохранение того же
  значения не создаёт копию, устаревшая кнопка не меняет анонс.

Тесты идут через настоящее приложение, FakeBot и временную SQLite.
"""

from __future__ import annotations

import unittest

try:
    from announcement_store import get_announcement_draft
    from handlers.announcement_wizard import DELIVERED_EDIT_TEXT
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        is_screen_record,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        _callback,
        _callbacks,
        _choose_personal,
        _latest_draft,
        _no_photos,
        _open_edit,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


# §18 (2.5): после доставки служебный экран — рабочий редактор анонса.
EDIT_ROW = ["📝 Данные", "🖼 Фото (0)"]
EDIT_MENU = "🏠 В меню"


def _details(h, draft_id: int):
    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
        )


def _keyboard_rows(h) -> list[list[tuple[str, str | None]]]:
    """Матрица inline-клавиатуры экрана: строки и кнопки."""
    record = h.screen()
    assert record is not None, "экрана ещё не было"
    markup = record.get("reply_markup")
    assert markup is not None, "у экрана нет клавиатуры"
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


def _post_ids(h, record: dict) -> tuple[int, ...]:
    """Фактические `message_id` сообщений, созданных этим постом."""
    return tuple(h.bot.call_message_ids.get(id(record), ()))


def _present_post_ids(h) -> list[int]:
    """
    `message_id` постов, которые РЕАЛЬНО остались в чате.

    Заменённые версии Telegram удаляет, поэтому их id в проверку порядка
    входить не должны — иначе тест требовал бы невозможного порядка от
    удалённого сообщения.
    """
    deleted = set(h.deleted_ids())
    out: list[int] = []
    for record in h.post_records():
        ids = _post_ids(h, record)
        if ids and all(message_id in deleted for message_id in ids):
            continue
        out.extend(ids)
    return out


def _final_markup(h, message_id: int):
    """Итоговая клавиатура сообщения (None — кнопок нет)."""
    calls = [
        call
        for call in h.calls
        if call["type"] == "edit_markup" and call.get("message_id") == message_id
    ]
    return calls[-1].get("reply_markup") if calls else None


def _screen_sends(h) -> list[dict]:
    """Созданные (не отредактированные) сообщения-экраны."""
    return [c for c in h.screens() if c["type"] == "message"]


async def _delivered(
    h,
    *,
    value: str = "Значение",
    code: str = "main",
    specs: list[tuple[str, str, str, dict[str, object]]] | None = None,
    content: str | None = None,
    extended_content: str | None = None,
):
    """Личный анонс, доставленный «Пропустить» на шаге фото."""
    place_id, _alias_id = _seed_place(h)
    if specs is None:
        specs = [(code, "Основное поле", "short_text", {})]
    template_id, _revision, bricks = _seed_personal_template(
        h, specs=specs, content=content, extended_content=extended_content
    )
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    await h.text(value)
    await _no_photos(h)
    if not h.screen_text().startswith(DELIVERED_EDIT_TEXT.split("\n\n")[0]):
        raise AssertionError(f"Анонс не доставлен: {h.screen_text()}")
    draft = _latest_draft(h)
    return draft, bricks


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DeliveredScreenTests(unittest.TestCase):
    """§18: после доставки — рабочий редактор, а не экран «Анонс готов»."""

    @scenario_test
    async def test_final_screen_is_editor_with_edit_categories(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)

        self.assertEqual(h.screen_text(), DELIVERED_EDIT_TEXT)
        self.assertNotIn("Анонс готов", h.screen_text())
        self.assertNotIn("Сообщение выше можно скопировать", h.screen_text())

        rows = _keyboard_rows(h)
        self.assertEqual([label for label, _data in rows[0]], EDIT_ROW, rows)
        self.assertEqual([label for label, _data in rows[-1]], [EDIT_MENU], rows)
        self.assertEqual(
            [data for _label, data in rows[0]],
            [
                f"aw_qmanage:{draft.id}:{draft.revision}",
                f"aw_photos:{draft.id}:{draft.revision}:0",
            ],
        )
        self.assertEqual(rows[-1][0][1], "main_menu")
        # Служебных сводок и кнопок прошлого экрана нет.
        for gone in ("aw_apply", "aw_preview", "aw_save", "aw_edit"):
            self.assertFalse(_callbacks(h, gone), f"лишняя кнопка {gone}")
        self.assertNotIn("Место:", h.screen_text())

    @scenario_test
    async def test_delivery_has_no_unapplied_changes_line(self, h) -> None:
        """H3: строка «есть изменения» больше не нужна — их не бывает."""
        await h.text("/start")
        draft, _bricks = await _delivered(h)

        self.assertEqual(h.screen_text(), DELIVERED_EDIT_TEXT)
        self.assertNotIn("Изменения пока не отправлены", h.screen_text())

        # Выход в меню: ложного «Назад» на экране правки нет.
        await h.callback("main_menu")
        self.assertIn("Что хотите сделать?", h.screen_text())

        # §29–30: повторное открытие из «Мои анонсы» публикует новый
        # экземпляр; автор снова попадает в редактор, а не на итоговый экран.
        await h.text("/start")
        await h.callback("my_announcements")
        await h.callback(f"aw_open:{draft.id}")
        self.assertEqual(h.screen_text(), DELIVERED_EDIT_TEXT)
        posts_after_reopen = h.posts()
        self.assertEqual(len(posts_after_reopen), 2, "новый экземпляр отправлен")

        # Правка значения применяется сразу, «неприменённых» изменений нет.
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        # Poster 2.4 (P2): «✏️ Изменить ответ» удалено — экран вопроса сам
        # принимает новый текст.
        await h.text("Новое значение")
        self.assertNotIn("Изменения пока не отправлены", h.screen_text())
        self.assertEqual(h.screen_text(), DELIVERED_EDIT_TEXT)
        self.assertEqual(len(h.posts()), 3, "новая версия ушла сразу")
        self.assertIn("Новое значение", h.posts()[-1] or "")
        self.assertEqual(_details(h, draft.id).publication_status, "sent")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DeliveredEditScreenTests(unittest.TestCase):
    """«✏️ Изменить» — компактный экран категорий без дублей (H3)."""

    # H3 (требование B): две категории в одной строке, условные доп. поля и
    # выход в главное меню. Ни «Применить изменения», ни «Обзор», ни
    # «Показать вложения», ни ложного «Назад».
    EDIT_ROW = ["📝 Данные", "🖼 Фото (0)"]
    EDIT_MENU = "🏠 В меню"

    @scenario_test
    async def test_edit_screen_lists_categories_without_sending_copies(self, h) -> None:
        await h.text("/start")
        # Poster 2.3.1 (E1): строка доп. полей есть только когда в
        # расширенном тексте шаблона стоит ещё не выбранный кирпичик.
        draft, _bricks = await _delivered(
            h,
            specs=[
                ("main", "Основное поле", "short_text", {}),
                ("extra_a", "Доп. поле", "short_text", {}),
            ],
            content="[main]",
            extended_content="[main]\n[extra_a]",
        )
        posts_before = h.posts()

        # §18 (2.5): экран категорий открыт сразу после доставки.
        self.assertEqual(h.screen_text(), DELIVERED_EDIT_TEXT)

        rows = _keyboard_rows(h)
        self.assertEqual([label for label, _data in rows[0]], self.EDIT_ROW, rows)
        self.assertEqual([label for label, _data in rows[-1]], [self.EDIT_MENU], rows)
        # «🧩 Дополнительные поля» — отдельная строка и только когда есть что
        # добавить: у этого шаблона доступные кирпичики есть.
        extras_rows = [
            row
            for row in rows
            if [label for label, _data in row] == ["🧩 Дополнительные поля"]
        ]
        self.assertEqual(len(extras_rows), 1, f"нет строки доп. полей: {rows}")
        labels = [label for label, _data in h.screen_buttons()]
        for gone in (
            "Применить изменения",
            "Обзор",
            "Показать вложения",
            "Назад",
        ):
            self.assertFalse(
                any(gone in label for label in labels), f"лишняя «{gone}»: {labels}"
            )
        # Экран правки ничего не отправляет и не меняет версию.
        self.assertEqual(h.posts(), posts_before)
        self.assertEqual(_details(h, draft.id).revision, draft.revision)

    @scenario_test
    async def test_extras_row_stays_when_all_extras_filled(self, h) -> None:
        """
        P2: строка доп. полей условная — она нужна, когда снимок содержит
        кирпичики расширенного текста (или уже выбранные доп. поля).
        Заполнение всех кирпичиков строку НЕ убирает: экран списка — точка
        входа к правке значений, поэтому список не может опустеть.
        """
        await h.text("/start")
        # Poster 2.3.1 (E1): доп. кирпичик доступен как кандидат только
        # там, где он стоит в расширенном тексте шаблона.
        draft, _bricks = await _delivered(
            h,
            specs=[
                ("main", "Основное поле", "short_text", {}),
                ("extra_a", "Доп. поле", "short_text", {}),
            ],
            content="[main]",
            extended_content="[main]\n[extra_a]",
        )
        self.assertIn(
            "🧩 Дополнительные поля",
            [label for label, _data in h.screen_buttons()],
        )

        # Заполняем все доступные кирпичики: P2 показывает и заполненные,
        # поэтому строка остаётся — иначе правка значения стала бы тупиком.
        from announcement_store import add_announcement_extra
        from announcement_wizard import extra_candidate_brick_ids

        with h.session_factory() as session:
            details = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
            snapshot = details.template_snapshot
            assert snapshot is not None, "у черновика должен быть снимок шаблона"
            candidates = extra_candidate_brick_ids(snapshot)
            self.assertTrue(candidates)
            for brick_id in candidates:
                add_announcement_extra(
                    session,
                    actor_telegram_user_id=DEFAULT_USER_ID,
                    draft_id=draft.id,
                    brick_id=brick_id,
                )

        await h.callback("main_menu")
        await h.text("/start")
        await h.callback("my_announcements")
        await h.callback(f"aw_open:{draft.id}")
        # Черновик возвращается на этап доп. полей: заполненный кирпичик
        # виден в списке и доступен для правки/удаления значения.
        if not _callbacks(h, "aw_exedit"):
            await _open_edit(h)
            await h.callback(_callback(h, "aw_extras"))
        self.assertTrue(
            _callbacks(h, "aw_exedit"),
            f"заполненное доп. поле должно остаться в списке: " f"{h.screen_buttons()}",
        )

    @scenario_test
    async def test_extras_row_hidden_without_extended_text(self, h) -> None:
        """Без расширенного текста и без выбранных полей строка скрыта."""
        await h.text("/start")
        draft, _bricks = await _delivered(
            h,
            specs=[("main", "Основное поле", "short_text", {})],
            content="[main]",
            extended_content=None,
        )
        self.assertNotIn(
            "🧩 Дополнительные поля",
            [label for label, _data in h.screen_buttons()],
        )

    @scenario_test
    async def test_main_menu_replaces_the_fake_back_button(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)

        # §18 (2.5): «✏️ Изменить» и «◀️ Назад» на экран завершения
        # (aw_edit/aw_panel) в клавиатуре больше нет — автор сразу на
        # категориях правки.
        self.assertFalse(_callbacks(h, "aw_edit"))
        self.assertFalse(_callbacks(h, "aw_panel"))
        self.assertTrue(_callbacks(h, "aw_qmanage"))
        await h.callback("main_menu")
        self.assertIn("Что хотите сделать?", h.screen_text())
        self.assertFalse(_callbacks(h, "aw_edit"))
        self.assertFalse(_callbacks(h, "aw_qmanage"))

    @scenario_test
    async def test_edit_categories_roundtrip_keeps_single_post(self, h) -> None:
        await h.text("/start")
        await _delivered(h)
        posts_before = len(h.posts())

        # §18 (2.5): категории правки уже на экране после доставки.
        self.assertEqual(h.screen_text(), DELIVERED_EDIT_TEXT)
        await h.callback(_callback(h, "aw_photos"))
        self.assertIn("Фотографии анонса", h.screen_text())
        # У доставленного анонса вложения уже стоят в чате выше: отдельного
        # «Показать вложения» нет, предпросмотр не дублирует готовый пост.
        self.assertEqual(len(h.posts()), posts_before)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DeliveredMenuTests(unittest.TestCase):
    """«🏠 В меню»: пост цел, служебный экран один."""

    @scenario_test
    async def test_menu_keeps_post_and_single_service_screen(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        post_ids = _present_post_ids(h)
        self.assertTrue(post_ids)
        screen_id = h.screen_message_id()
        self.assertIsNotNone(screen_id)
        sends_before = len(_screen_sends(h))

        await h.callback("main_menu")

        self.assertIn("Что хотите сделать?", h.screen_text())
        self.assertFalse(_callbacks(h, "aw_edit"), "экран завершения закрыт")
        # Пост не удалён и не переотправлен.
        for message_id in post_ids:
            self.assertNotIn(message_id, h.deleted_ids())
        self.assertEqual(_present_post_ids(h), post_ids)
        self.assertEqual(len(h.posts()), 1)
        # Второго служебного экрана не появилось: меню встало в тот же.
        self.assertEqual(len(_screen_sends(h)), sends_before)
        self.assertEqual(h.screen_message_id(), screen_id)

        # Прошлый анонс не тронут и в БД.
        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(len(stored.delivered_message_ids), 1)

    @scenario_test
    async def test_menu_does_not_touch_earlier_announcements(self, h) -> None:
        await h.text("/start")
        first, _bricks = await _delivered(h, value="Первый")
        first_ids = set(_details(h, first.id).delivered_message_ids)

        await h.callback("main_menu")
        await h.text("/start")
        second, _bricks2 = await _delivered(
            h,
            value="Другой",
            specs=[("second", "Второе поле", "short_text", {})],
        )
        second_ids = set(_details(h, second.id).delivered_message_ids)
        self.assertNotEqual(second.id, first.id)
        self.assertFalse(second_ids & first_ids)

        await h.callback("main_menu")
        self.assertFalse(set(first_ids) & set(h.deleted_ids()))
        self.assertEqual(set(_details(h, first.id).delivered_message_ids), first_ids)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class MessageOrderInvariantTests(unittest.TestCase):
    """Все реальные посты выше служебного экрана — по фактическим id."""

    def _assert_order(self, h) -> None:
        post_ids = _present_post_ids(h)
        screen_id = h.screen_message_id()
        self.assertTrue(post_ids, "нет ни одного поста")
        self.assertIsNotNone(screen_id, "нет служебного экрана")
        assert screen_id is not None
        self.assertLess(
            max(post_ids),
            screen_id,
            f"пост должен быть выше экрана: posts={post_ids}, screen={screen_id}",
        )
        # Экран, в который рисуется UI, — именно тот, что ниже поста.
        last_screen = h.screens()[-1]
        self.assertTrue(is_screen_record(last_screen))

    @scenario_test
    async def test_first_delivery_puts_screen_below_post(self, h) -> None:
        await h.text("/start")
        await _delivered(h)
        self._assert_order(h)

    @scenario_test
    async def test_replacement_version_keeps_screen_below_post(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        first_ids = _details(h, draft.id).delivered_message_ids

        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        # Poster 2.4 (P2): «✏️ Изменить ответ» удалено — экран вопроса сам
        # принимает новый текст.
        await h.text("Обновлено")
        # H3: новая версия ушла сразу, без «Применить изменения».

        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(tuple(stored.previous_message_ids), first_ids)
        self.assertIn("Обновлено", h.posts()[-1] or "")
        # §30–31: прежняя версия остаётся в чате архивным снимком без кнопок,
        # а не удаляется: история доставок сохраняется.
        self.assertFalse(
            set(first_ids) & set(h.deleted_ids()),
            "прежний экземпляр не удаляется",
        )
        for message_id in first_ids:
            self.assertIsNone(
                _final_markup(h, message_id),
                "с прежнего экземпляра снята клавиатура",
            )
        self._assert_order(h)

        # Возврат в меню и повторное открытие не ломают порядок и не плодят
        # второе сообщение экрана.
        sends_before = len(_screen_sends(h))
        await h.callback("main_menu")
        self.assertEqual(len(_screen_sends(h)), sends_before)
        self._assert_order(h)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DeliveredApplyTests(unittest.TestCase):
    """H3: применения «по кнопке» больше нет; важна идемпотентность."""

    @scenario_test
    async def test_visiting_edit_screens_does_not_create_a_post(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        posts_before = h.posts()
        version_ids = _details(h, draft.id).delivered_message_ids

        await _open_edit(h)
        await h.callback(_callback(h, "aw_photos"))
        await h.callback("main_menu")

        # Осмотр экранов правки не публикует копию и не меняет экземпляр.
        self.assertEqual(h.posts(), posts_before)
        stored = _details(h, draft.id)
        self.assertEqual(tuple(stored.delivered_message_ids), version_ids)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(len(_present_post_ids(h)), len(version_ids))
        # Кнопки применения на экране правки нет вовсе.
        self.assertFalse(_callbacks(h, "aw_apply"))

        # §29–30 (2.5): повторное открытие из «Мои анонсы» — единственное
        # действие, которое публикует новый экземпляр, и ровно один.
        await h.text("/start")
        await h.callback("my_announcements")
        await h.callback(f"aw_open:{draft.id}")

        self.assertEqual(len(h.posts()), len(posts_before) + 1, "один новый пост")
        self.assertEqual(h.posts()[:-1], posts_before)
        self.assertEqual(h.posts()[-1], posts_before[-1], "текст экземпляра тот же")
        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(tuple(stored.previous_message_ids), version_ids)
        self.assertNotEqual(
            tuple(stored.delivered_message_ids),
            version_ids,
            "активен новый экземпляр",
        )
        self.assertEqual(h.screen_text(), DELIVERED_EDIT_TEXT)

    @scenario_test
    async def test_success_shows_editor_not_stale_notice(self, h) -> None:
        await h.text("/start")
        await _delivered(h)
        # Обычный успех: экран — рабочий редактор, без «Данные изменились.».
        self.assertEqual(h.screen_text(), DELIVERED_EDIT_TEXT)
        self.assertNotIn("Данные изменились.", h.screen_text())
        self.assertNotIn("Данные изменились.", h.answer_texts())

    @scenario_test
    async def test_stale_edit_button_only_warns_about_old_action(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        # Кнопки «✏️ Изменить» больше нет; проверяем именно устаревший
        # callback прежней версии экрана.
        stale_edit = f"aw_edit:{draft.id}:{draft.revision}"

        # Правка меняет ревизию: старая кнопка «Изменить» устарела.
        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        # Poster 2.4 (P2): «✏️ Изменить ответ» удалено — экран вопроса сам
        # принимает новый текст.
        await h.text("Другое значение")

        await h.callback(stale_edit)
        self.assertIn("Данные изменились.", h.answer_texts()[-1])
        # Экран остаётся рабочим редактором: устаревший callback не отправил
        # вторую копию и не открыл чужую версию правки.
        self.assertIn(DELIVERED_EDIT_TEXT, h.screen_text())
        rows = _keyboard_rows(h)
        self.assertEqual([label for label, _data in rows[0]], EDIT_ROW, rows)
        self.assertEqual([label for label, _data in rows[-1]], [EDIT_MENU], rows)
        # Одна новая версия от самой правки (H3: применяется сразу), не две.
        self.assertEqual(len(h.posts()), 2)
        self.assertIn("Другое значение", h.posts()[-1] or "")
        self.assertEqual(_details(h, draft.id).publication_status, "sent")

    @scenario_test
    async def test_legacy_panel_button_opens_editor_without_new_post(self, h) -> None:
        """§18 (2.5): «◀️ Назад» прежних версий (`aw_panel`) ведёт в редактор."""
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        posts_before = h.posts()

        await h.callback(f"aw_panel:{draft.id}:{draft.revision}")

        self.assertEqual(h.screen_text(), DELIVERED_EDIT_TEXT)
        self.assertEqual([label for label, _data in _keyboard_rows(h)[0]], EDIT_ROW)
        self.assertEqual(h.posts(), posts_before, "устаревшая кнопка не публикует")

    @scenario_test
    async def test_repeated_stale_callback_changes_nothing(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        stale_edit = f"aw_edit:{draft.id}:{draft.revision}"
        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        # Poster 2.4 (P2): «✏️ Изменить ответ» удалено — экран вопроса сам
        # принимает новый текст.
        await h.text("Другое")

        await h.callback(stale_edit)
        first_text = h.screen_text()
        posts_after_first = h.posts()

        await h.callback(stale_edit)
        self.assertIn("Данные изменились.", h.answer_texts()[-1])
        self.assertEqual(h.screen_text(), first_text)
        self.assertEqual(h.posts(), posts_after_first)
