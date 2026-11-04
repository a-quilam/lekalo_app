# tests/test_stage252_single_control.py
"""
Poster 2.5.2, T1: одна публикация — одно место управления (§2, сценарии 1–7).

Проверяется пользовательский результат через настоящее приложение, FakeBot и
временную SQLite (реальная `post_bot.db`, `.env` и сеть не используются):

- **0 фото** — ровно одно сообщение анонса с клавиатурой управления; экрана
  «✏️ Изменить анонс» и отдельной панели в чате НЕТ.
- **1 фото** — один `sendPhoto`; управление на том же сообщении, второго
  набора кнопок нет.
- **2–10 фото** — один `sendMediaGroup` и РОВНО одна отдельная панель;
  экрана редактирования нет.
- **«🏠 В меню»** — из панели и из временного экрана редактирования:
  экземпляр архивный, клавиатура снята с фактического сообщения анонса
  (сообщение остаётся в чате), панель медиагруппы закрыта, состояние
  диалога очищено, открыто главное меню. Кнопки архивного экземпляра —
  безопасные no-op (alert, данные не меняются).
- **Правка** — успешное сохранение обновляет анонс на месте, временный экран
  удаляется, управление остаётся у анонса; неудачный ввод экран не удаляет.
"""

from __future__ import annotations

import unittest

try:
    import announcement_controls as controls
    from announcement_instances import get_instance
    from handlers.announcement_wizard import AW_PREVIEW, CONVERSATION_NAME
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        inline_buttons,
        is_panel_record,
        scenario_test,
    )
    from tests.test_stage25_delivery_instances import (
        _delivered_with_photos,
        _delivered_without_photos,
        _final_markup,
        _only_active,
        _stored,
    )
    from tests.test_stage27b_announcement_ui import (
        _callback,
        _choose_personal,
        _latest_draft,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


EDIT_TITLE = "✏️ Изменить анонс"
MAIN_MENU_TITLE = "Здравствуйте! 👋"
PHOTO_STEP_TITLE = "📷 Фотографии анонса"
USER_ID = 1


# --- помощники -------------------------------------------------------------


def _control_keyboards(h, *prefixes: str) -> list[int]:
    """
    `message_id` ЖИВЫХ сообщений чата, несущих клавиатуру с этими callback.

    FakeBot хранит итоговую подпись клавиатуры каждого сообщения, поэтому
    проверяется фактический чат, а не журнал вызовов: удалённые служебные
    сообщения сюда не попадают.
    """
    deleted = {int(mid) for mid in h.deleted_ids()}
    found: list[int] = []
    for (_chat_id, message_id), (
        _kind,
        _text,
        markup,
    ) in h.bot._message_content.items():
        if int(message_id) in deleted or not markup:
            continue
        if any(prefix in markup for prefix in prefixes):
            found.append(int(message_id))
    return sorted(found)


def _edits_of(h, message_id: int, *, since: int = 0) -> list[dict]:
    """
    Записи правок СОДЕРЖИМОГО сообщения (`editMessageText/Caption/Media`).

    Нужны для проверки инварианта «правка анонса не снимает управление»:
    продукт обязан передавать актуальную клавиатуру явно, а не полагаться
    на то, что Telegram её сохранит. `since` — индекс в журнале вызовов,
    с которого начинается проверяемое действие (правки служебных экранов
    до него не относятся к анонсу).
    """
    kinds = ("edit_message", "edit_caption", "edit_media")
    return [
        call
        for call in h.calls[since:]
        if call["type"] in kinds
        and call.get("message_id") is not None
        and int(call["message_id"]) == int(message_id)
    ]


def _panel_records(h) -> list[dict]:
    return [call for call in h.calls if is_panel_record(call)]


def _panel_message_id(h) -> int | None:
    panels = _panel_records(h)
    if not panels:
        return None
    ids = h.bot.call_message_ids.get(id(panels[-1]), ())
    return int(ids[0]) if ids else None


def _instance(h, instance_id: int):
    with h.session_factory() as session:
        return get_instance(session, instance_id)


def _main_menu_shown(h) -> bool:
    return h.screen_text().startswith(MAIN_MENU_TITLE)


# --- 1–3. публикация: одно место управления --------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class SingleControlAfterPublicationTests(unittest.TestCase):
    @scenario_test
    async def test_zero_photos_control_only_on_announcement(self, h) -> None:
        """§20: одно сообщение с текстом и управлением, без дубля экрана."""
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        stored = _stored(h, draft.id)
        content_id = stored.delivered_message_ids[0]

        markup = _final_markup(h, content_id)
        self.assertIsNotNone(markup, "у анонса нет клавиатуры управления")
        self.assertTrue(
            all(
                data and data.startswith("anp_") for _l, data in inline_buttons(markup)
            ),
            inline_buttons(markup),
        )
        self.assertEqual(_control_keyboards(h, "anp_"), [content_id])
        # Дублирующего экрана редактирования в чате нет.
        self.assertNotIn(EDIT_TITLE, h.screen_text())
        self.assertEqual(_panel_records(h), [], "отдельная панель не нужна")

    @scenario_test
    async def test_one_photo_control_only_on_announcement(self, h) -> None:
        """§21: один sendPhoto с подписью и управлением, без второго набора."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 1)
        stored = _stored(h, draft.id)
        content_id = stored.delivered_message_ids[0]

        photos = [call for call in h.calls if call["type"] == "photo"]
        self.assertEqual(len(photos), 1, "ровно одна отправка фото")
        self.assertEqual(photos[0]["caption"], stored.publication_text)
        markup = _final_markup(h, content_id)
        self.assertIsNotNone(markup)
        self.assertTrue(
            all(data and data.startswith("anp_") for _l, data in inline_buttons(markup))
        )
        self.assertEqual(_control_keyboards(h, "anp_"), [content_id])
        self.assertNotIn(EDIT_TITLE, h.screen_text())
        self.assertEqual(_panel_records(h), [])

    @scenario_test
    async def test_album_has_exactly_one_panel(self, h) -> None:
        """§22: альбом + ровно одна панель управления, без экрана редактора."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 3)
        stored = _stored(h, draft.id)

        groups = [call for call in h.calls if call["type"] == "media_group"]
        self.assertEqual(len(groups), 1, "ровно одна медиагруппа")
        panels = _panel_records(h)
        self.assertEqual(len(panels), 1, f"панелей: {len(panels)}")
        panel_id = _panel_message_id(h)
        self.assertIsNotNone(panel_id)
        # Единственное место управления — панель, дубля экрана нет.
        self.assertEqual(_control_keyboards(h, "anp_"), [panel_id])
        self.assertNotIn(EDIT_TITLE, h.screen_text())
        active = _only_active(h, draft.id)
        self.assertEqual(active.message_ids, tuple(stored.delivered_message_ids))


# --- 4. «🏠 В меню» из панели ---------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PanelExitToMenuTests(unittest.TestCase):
    @scenario_test
    async def test_panel_menu_archives_and_strips_announcement(self, h) -> None:
        """§30 п.4–5: экземпляр архивный, кнопки сняты, сообщение осталось."""
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        active = _only_active(h, draft.id)
        message_id = active.message_ids[0]
        posts_before = h.posts()
        screens_before = len(h.calls)
        self.assertEqual(screens_before, len(h.calls))

        await h.callback(controls.panel_callback(controls.ACTION_MENU, active.id))

        archived = _instance(h, active.id)
        self.assertIsNotNone(archived)
        assert archived is not None
        self.assertTrue(archived.is_archived, "экземпляр не архивирован")
        self.assertIsNone(
            _final_markup(h, message_id), "клавиатура анонса должна быть снята"
        )
        self.assertNotIn(message_id, h.deleted_ids(), "сообщение анонса не удаляется")
        self.assertEqual(h.posts(), posts_before, "анонс не переиздавался")
        self.assertTrue(_main_menu_shown(h), h.screen_text()[:80])
        self.assertIsNone(
            h.dialog_state(name=CONVERSATION_NAME), "состояние диалога очищено"
        )

        # Старые кнопки прежнего экземпляра — безопасный no-op.
        calls_before = len(h.calls)
        answers_before = len(h.answers())
        await h.callback(controls.panel_callback(controls.ACTION_DATA, active.id))
        self.assertEqual(
            [
                call
                for call in h.calls[calls_before:]
                if call["type"] != "callback_answer"
            ],
            [],
            "устаревшая кнопка не должна ничего менять",
        )
        answers = h.answers()[answers_before:]
        self.assertEqual(len(answers), 1)
        self.assertTrue(answers[0].get("show_alert"))
        self.assertEqual(answers[0].get("text"), controls.STALE_INSTANCE_TEXT)

    @scenario_test
    async def test_panel_menu_closes_media_group_panel(self, h) -> None:
        """§30 п.5: панель прежней медиагруппы закрывается, альбом остаётся."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 2)
        active = _only_active(h, draft.id)
        panel_id = active.panel_message_id
        self.assertIsNotNone(panel_id)
        album_ids = tuple(active.message_ids)

        await h.callback(controls.panel_callback(controls.ACTION_MENU, active.id))

        archived = _instance(h, active.id)
        assert archived is not None
        self.assertTrue(archived.is_archived)
        self.assertIn(int(panel_id), h.deleted_ids(), "панель должна быть закрыта")
        self.assertIsNone(archived.panel_message_id, "адрес панели снят")
        self.assertFalse(
            set(album_ids) & set(h.deleted_ids()),
            "вложения альбома остаются в чате",
        )
        self.assertTrue(_main_menu_shown(h))
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))


# --- 4. «🏠 В меню» из временного экрана редактирования --------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class EditorExitToMenuTests(unittest.TestCase):
    @scenario_test
    async def test_editor_menu_archives_and_strips_announcement(self, h) -> None:
        """Выход из редактора: экран управление снимут, меню открыто."""
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        active = _only_active(h, draft.id)
        message_id = active.message_ids[0]

        await h.callback(controls.panel_callback(controls.ACTION_DATA, active.id))
        self.assertTrue(h.screen_text().startswith(EDIT_TITLE), h.screen_text()[:80])

        await h.callback("main_menu")

        archived = _instance(h, active.id)
        assert archived is not None
        self.assertTrue(archived.is_archived)
        self.assertIsNone(_final_markup(h, message_id))
        self.assertNotIn(message_id, h.deleted_ids())
        self.assertTrue(_main_menu_shown(h))
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))


# --- 7. временные экраны редактирования ------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TemporaryEditorTests(unittest.TestCase):
    @scenario_test
    async def test_successful_edit_closes_screen_and_keeps_control(self, h) -> None:
        """Успешное сохранение: анонс на месте, экран удалён, управление у поста."""
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        active = _only_active(h, draft.id)
        message_id = active.message_ids[0]

        await h.callback(controls.panel_callback(controls.ACTION_DATA, active.id))
        screen_id = h.screen_message_id()
        self.assertIsNotNone(screen_id)

        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        mark = len(h.calls)
        await h.text("Обновлено")

        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), "Обновлено", "правка не на месте"
        )
        self.assertIn(int(screen_id), h.deleted_ids(), "временный экран не закрыт")
        markup = _final_markup(h, message_id)
        self.assertIsNotNone(markup, "управление не вернулось к анонсу")
        self.assertTrue(
            all(data and data.startswith("anp_") for _l, data in inline_buttons(markup))
        )
        # Контракт не должен зависеть от семантики Bot API: правка содержимого
        # передаёт актуальную клавиатуру явно, поэтому управление не теряется
        # ни в реальном Telegram, ни в FakeBot.
        edits = _edits_of(h, message_id, since=mark)
        self.assertTrue(edits, "правка анонса не записана")
        for call in edits:
            passed = inline_buttons(call.get("reply_markup"))
            self.assertTrue(
                passed and all(data and data.startswith("anp_") for _l, data in passed),
                f"правка анонса без клавиатуры управления: {call['type']}, {passed}",
            )
        self.assertEqual(h.posts(), ["Обновлено"], "вторая версия не отправлена")
        self.assertEqual(len(h.answers()) >= 1, True)
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PREVIEW)

    @scenario_test
    async def test_successful_edit_of_photo_keeps_control(self, h) -> None:
        """1 фото: правка подписи тоже явно несёт клавиатуру управления."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 1)
        active = _only_active(h, draft.id)
        message_id = active.message_ids[0]

        await h.callback(controls.panel_callback(controls.ACTION_DATA, active.id))
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        mark = len(h.calls)
        await h.text("Обновлено")

        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            "Обновлено",
            "подпись не на месте",
        )
        edits = _edits_of(h, message_id, since=mark)
        self.assertTrue(edits, "правка подписи не записана")
        for call in edits:
            passed = inline_buttons(call.get("reply_markup"))
            self.assertTrue(
                passed and all(data and data.startswith("anp_") for _l, data in passed),
                f"правка подписи без клавиатуры управления: {passed}",
            )
        self.assertIsNotNone(_final_markup(h, message_id), "клавиатура потеряна")
        self.assertEqual(h.posts(), ["Значение"], "вторая версия не отправлена")

    @scenario_test
    async def test_failed_input_keeps_editor_screen(self, h) -> None:
        """Неудачный ввод: сообщение экрана остаётся, данные не потеряны."""
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h, specs=[("main", "Основное поле", "number", {})]
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("42")
        if PHOTO_STEP_TITLE not in h.screen_text():
            raise AssertionError(f"Не шаг фото: {h.screen_text()}")
        await h.callback(_callback(h, "aw_photo_none"))
        draft = _latest_draft(h)
        active = _only_active(h, draft.id)
        message_id = active.message_ids[0]

        await h.callback(controls.panel_callback(controls.ACTION_DATA, active.id))
        screen_id = h.screen_message_id()
        self.assertIsNotNone(screen_id, "экран редактора не открыт")
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.text("не число")

        self.assertNotIn(
            int(screen_id), h.deleted_ids(), "экран неудачного ввода не удаляется"
        )
        self.assertIn("число", h.screen_text().lower(), h.screen_text()[:120])
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            "42",
            "анонс не изменился при неудачном вводе",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
