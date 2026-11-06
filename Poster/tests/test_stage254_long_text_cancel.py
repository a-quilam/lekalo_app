# tests/test_stage254_long_text_cancel.py
"""
Poster 2.5.4 §3 — длинный текст и отмена незавершённой правки.

Проверяется пользовательский результат через настоящее приложение, FakeBot и
временную SQLite (`tests/integration_harness.py`): реальная `post_bot.db`,
`.env` и сеть Telegram не используются.

Что закрепляется:

C. Без фото (>4096). Правка поля доставленного анонса непоместившимся
   текстом: анонс и его сообщение целы, текст сохранён как незавершённая
   правка, сообщение пользователя не удалено, на управлении РОВНО две кнопки
   (неактивная подсказка «⏳ Ожидаю текст короче» + «Отменить»), лимит назван
   числом и про фотографии не упоминается (их нет).
D. Одно фото (подпись >1024). Опубликованный анонс не изменился, правка
   пережила рестарт службы, а исправление применилось РОВНО одним
   `editMessageCaption`; второй панели не появилось.
D2. Медиагруппа (2+ фото). Экран ошибки рисуется в ЕДИНСТВЕННОЙ панели
   (`editMessageText`), второй панели и новых сообщений нет, публикация и
   сообщение анонса не меняются.
E. «Отменить» возвращает прежнее значение поля, не трогает другие поля и
   закрывает экран ошибки (список полей), а повторное/позднее нажатие
   безопасно: новое значение не откатывается.
Гонка: поздний callback старой отмены после успешного сохранения ничего не
   пишет (ревизия/отметка правки уже не совпадают).
Лимиты: 4096/1024 считаются по ВИДИМОМУ тексту после разбора разметки.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage254_long_text_cancel -v
"""

from __future__ import annotations

import asyncio
import unittest

try:
    import announcement_controls as controls
    from template_compiler import (
        ANNOUNCEMENT_CAPTION_LIMIT,
        ANNOUNCEMENT_TEXT_LIMIT,
        check_announcement_length,
    )
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        is_panel_record,
        scenario_test,
    )
    from tests.test_stage25_delivery_instances import (
        _only_active,
        _panel_sends,
        _stored,
    )
    from tests.test_stage27b_announcement_ui import (
        _choose_personal,
        _latest_draft,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )
    from tests.test_stage254_field_edit_inplace import (
        _created_count,
        _field_buttons,
        _input_markup,
    )
    from utils.inline_buttons import is_disabled
    from utils.ui_texts import tr

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


PHOTOS_TITLE = tr("wizard.photos.title")
# Короткая причина для ДОСТАВЛЕННОГО анонса (2.5.4 §3) — без «уберите фото».
EDIT_REFUSAL = "Анонс не обновлён: текст не помещается"
HINT = tr("wizard.length_refusal.edit_hint")
CANCEL = tr("wizard.length_refusal.edit_cancel")

OLD_DESCRIPTION = "Старое описание"
SECOND_VALUE = "Подзаголовок"
FIXED_DESCRIPTION = "Исправленное описание"

# 4096 — предел текста без фото, 1024 — подписи к фото. Значения берутся с
# запасом, чтобы шаблонная обвязка («Строка 0: ») не решала исход.
LONG_NO_PHOTO = "а" * 4200
LONG_CAPTION = "б" * 1100


# --- помощники -------------------------------------------------------------


async def _delivered(h, *, photos: int):
    """Два поля («Описание» long_text, «Подзаголовок») и доставка анонса."""
    place_id, _alias = _seed_place(h)
    specs: list[tuple[str, str, str, dict[str, object]]] = [
        ("f0", "Описание", "long_text", {}),
        ("f1", "Подзаголовок", "short_text", {}),
    ]
    content = "Строка 0: [f0]\nСтрока 1: [f1]"
    template_id, _revision, bricks = _seed_personal_template(
        h, name="Шаблон §3", specs=specs, content=content
    )
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    answers = [OLD_DESCRIPTION, SECOND_VALUE]
    for index in range(len(specs) + 5):
        if PHOTOS_TITLE in h.screen_text():
            break
        await h.text(answers[index] if index < len(answers) else "Значение")
    if PHOTOS_TITLE not in h.screen_text():
        raise AssertionError(f"Не шаг фото: {h.screen_text()[:200]!r}")
    if photos == 0:
        await h.callback(_photo_none(h))
    elif photos == 1:
        await h.photo("photo-0")
    else:
        # 2+ фото — медиагруппа: у неё нет общей клавиатуры, управление
        # живёт в единственной панели.
        for index in range(photos):
            await h.photo(f"photo-{index}", media_group_id="album")
        await asyncio.sleep(0.4)
    draft = _latest_draft(h)
    if _stored(h, draft.id).publication_status != "sent":
        raise AssertionError(f"анонс не доставлен: {h.screen_text()[:200]!r}")
    return draft, bricks


def _photo_none(h) -> str:
    for _label, data in h.screen_buttons():
        if data and data.startswith("aw_photo_none"):
            return data
    raise AssertionError(f"нет «Без фотографий»: {h.screen_buttons()}")


async def _open_list(h, instance_id: int) -> None:
    await h.callback(controls.panel_callback(controls.ACTION_DATA, instance_id))


async def _open_description(h, instance_id: int) -> None:
    """Открывает ввод поля «Описание» на управлении анонса."""
    if not _field_buttons(h):
        await _open_list(h, instance_id)
    data = next(data for label, data in _field_buttons(h) if "Описание" in label)
    await h.callback(data)


def _message_id(h, draft_id: int) -> int:
    return int(_only_active(h, draft_id).message_ids[0])


def _error_keyboard(h, message_id: int):
    """Строки клавиатуры, показанной на сообщении анонса в последний раз."""
    markup = _input_markup(h, message_id)
    if markup is None or not hasattr(markup, "inline_keyboard"):
        raise AssertionError("клавиатура анонса не обновлена")
    return list(markup.inline_keyboard or [])


def _edit_refusals(h) -> list[str]:
    """Короткие причины ошибки длины, показанные автору."""
    return [text for text in h.reply_texts() if EDIT_REFUSAL in text]


def _caption_edits(h) -> list[dict]:
    return [call for call in h.calls if call["type"] == "edit_caption"]


# --- C. без фото: >4096, анонс цел, ровно две кнопки -----------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class LongTextWithoutPhotosTests(unittest.TestCase):
    """§3: непоместившийся текст не публикуется и не теряется."""

    @scenario_test
    async def test_c_no_photo_long_text_keeps_announcement_and_input(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=0)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)
        published = _stored(h, draft.id).publication_text
        self.assertEqual(
            published, f"Строка 0: {OLD_DESCRIPTION}\nСтрока 1: {SECOND_VALUE}"
        )
        posts_before = len(h.posts())

        await _open_description(h, active.id)
        created_before = _created_count(h)
        long_message = await h.text(LONG_NO_PHOTO)

        # Причина: лимит назван числом, про фотографии речи нет.
        shown = "\n".join(_edit_refusals(h))
        self.assertIn(EDIT_REFUSAL, shown, f"нет ошибки длины: {h.reply_texts()}")
        self.assertIn(f"лимите {ANNOUNCEMENT_TEXT_LIMIT}", shown)
        self.assertNotIn("фото", shown.lower(), "без фото про фото не пишем")

        # На управлении анонса — РОВНО две кнопки: подсказка и «Отменить».
        rows = _error_keyboard(h, message_id)
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual(len(rows[0]), 1, rows[0])
        self.assertTrue(is_disabled(rows[0][0]), "подсказка должна быть неактивной")
        self.assertEqual(rows[0][0].text, HINT)
        self.assertIsNone(rows[0][0].callback_data)
        self.assertEqual(len(rows[1]), 1, rows[1])
        cancel = rows[1][0]
        self.assertEqual(cancel.text, CANCEL)
        self.assertTrue(cancel.callback_data.startswith("anq_cancel:"), cancel)
        # Никаких «Назад», «Пропустить», списков полей и «Обзора».
        labels = " | ".join(button.text for row in rows for button in row)
        for marker in ("Назад", "Пропустить", "Обзор", "фото"):
            self.assertNotIn(marker, labels, labels)

        # Анонс и его сообщение НЕ менялись, ввода автор не лишился.
        self.assertNotIn(long_message.message.message_id, h.deleted_ids())
        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.publication_text, published, "анонс переиздан")
        self.assertEqual(stored.delivered_message_ids, (message_id,))
        self.assertEqual(h.bot.content_text(h.chat_id, message_id), published)
        self.assertEqual(len(h.posts()), posts_before, "лишняя публикация")
        # Новых сообщений интерфейса не появилось (кроме ответа на ввод).
        self.assertEqual(
            _created_count(h), created_before + 1, "лишние сообщения интерфейса"
        )
        # Ввод целиком в незавершённой правке: переживёт рестарт штатно.
        value_row = next(
            row for row in _value_rows(h, draft.id) if row[0] == bricks["f0"]
        )
        self.assertEqual(value_row[1], "valid")
        self.assertIn(LONG_NO_PHOTO, value_row[2] or "", "сохранён не весь текст")


# --- C3. висящая длинная правка не предлагает убрать фото ------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PendingLongEditMessageTests(unittest.TestCase):
    """
    §3.3: пока висит незавершённая длинная правка, смена ДРУГОГО поля
    получает короткую причину без посторонних действий.

    Находка приёмки: у анонса БЕЗ фотографий совет «уберите фото» запрещён
    (убирать нечего). Совет зависит от фактического вида сообщения.
    """

    @scenario_test
    async def test_date_change_advice_has_no_photo_hint(self, h) -> None:
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        specs: list[tuple[str, str, str, dict[str, object]]] = [
            ("f0", "Описание", "long_text", {}),
            ("f1", "Дата", "date", {}),
        ]
        template_id, _revision, bricks = _seed_personal_template(
            h, name="Шаблон §3.3", specs=specs, content="Строка 0: [f0]\nДата: [f1]"
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        for _ in range(len(specs) + 5):
            if PHOTOS_TITLE in h.screen_text():
                break
            buttons = [data for _label, data in h.screen_buttons()]
            day = next((data for data in buttons if data.startswith("aw_calday")), None)
            if day is not None:
                await h.callback(day)
                continue
            await h.text(OLD_DESCRIPTION if len(h.reply_texts()) <= 0 else "Значение")
        if PHOTOS_TITLE not in h.screen_text():
            raise AssertionError(f"Не шаг фото: {h.screen_text()[:200]!r}")
        await h.callback(_photo_none(h))
        draft = _latest_draft(h)
        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.publication_photos, (), "анонс должен быть без фото")
        published = stored.publication_text

        active = _only_active(h, draft.id)
        await _open_description(h, active.id)
        await h.text(LONG_NO_PHOTO)
        self.assertTrue(_edit_refusals(h), "экран ошибки длины не показан")

        # Автор открывает ДРУГОЕ поле (дата) старой кнопкой списка и меняет
        # день: публикации нет, но и совет «уберите фото» неуместен.
        await h.callback(f"anf_sel:{active.id}:{bricks['f1']}")
        days = [
            data
            for _label, data in h.live_buttons()
            if data and data.startswith("anq_day:")
        ]
        self.assertTrue(days, f"календаря нет: {h.live_buttons()}")
        await h.callback(days[0])

        shown = "\n".join(
            text for text in h.answer_texts() if "текст не помещается" in text
        )
        self.assertTrue(shown, f"нет причины: {h.answer_texts()}")
        self.assertIn(f"лимите {ANNOUNCEMENT_TEXT_LIMIT}", shown)
        low = shown.lower()
        self.assertNotIn("фото", low, f"совет про фото без фото: {shown!r}")
        self.assertNotIn("уберите", low, f"постороннее действие в совете: {shown!r}")
        # Публикация и сообщение анонса не изменились.
        after = _stored(h, draft.id)
        self.assertEqual(after.publication_text, published, "анонс переиздан")
        self.assertEqual(after.publication_status, "sent")


# --- D. одно фото: подпись >1024, рестарт, одно обновление -----------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class LongCaptionWithPhotoTests(unittest.TestCase):
    """§3: лимит подписи 1024, правка переживает рестарт и применяется раз."""

    @scenario_test
    async def test_d_caption_limit_restart_and_single_update(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)
        published = _stored(h, draft.id).publication_text
        self.assertTrue(published)
        self.assertIsNone(active.panel_message_id, "у одного фото панели нет")
        posts_before = len(h.posts())

        await _open_description(h, active.id)
        await h.text(LONG_CAPTION)

        shown = "\n".join(_edit_refusals(h))
        self.assertIn(EDIT_REFUSAL, shown)
        self.assertIn(f"лимите {ANNOUNCEMENT_CAPTION_LIMIT}", shown)
        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_text, published, "подпись изменилась")
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), published, "сообщение изменено"
        )
        self.assertEqual(len(h.posts()), posts_before, "лишняя публикация")
        value_row = next(
            row for row in _value_rows(h, draft.id) if row[0] == bricks["f0"]
        )
        self.assertIn(LONG_CAPTION, value_row[2] or "", "правка не сохранена")

        # Рестарт службы: незавершённая правка и снимок отмены живут в БД.
        await h.restart()
        self.assertIsNone(h.user_data().get("announcement_ui"))
        value_row = next(
            row for row in _value_rows(h, draft.id) if row[0] == bricks["f0"]
        )
        self.assertIn(LONG_CAPTION, value_row[2] or "", "правка не пережила рестарт")
        self.assertEqual(_stored(h, draft.id).publication_text, published)

        # Исправление: ровно одно обновление подписи того же сообщения.
        edits_before = len(_caption_edits(h))
        created_before = _created_count(h)
        await h.text(FIXED_DESCRIPTION)

        fixed = _stored(h, draft.id)
        self.assertEqual(fixed.publication_status, "sent")
        self.assertIn(FIXED_DESCRIPTION, fixed.publication_text or "")
        self.assertNotIn(LONG_CAPTION, fixed.publication_text or "")
        self.assertEqual(fixed.delivered_message_ids, (message_id,), "анонс переиздан")
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), fixed.publication_text
        )
        self.assertEqual(
            len(_caption_edits(h)) - edits_before, 1, "должно быть одно обновление"
        )
        self.assertEqual(len(h.posts()), posts_before, "лишняя публикация")
        self.assertEqual(_created_count(h), created_before, "лишние сообщения")
        self.assertIsNone(
            _only_active(h, draft.id).panel_message_id, "появилась вторая панель"
        )


# --- C2. медиагруппа: ошибка длины в единственной панели -------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class LongCaptionInMediaGroupTests(unittest.TestCase):
    """§3: у 2+ фото экран ошибки живёт в единственной панели, не в новом экране."""

    @scenario_test
    async def test_media_group_error_uses_single_panel(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=2)
        active = _only_active(h, draft.id)
        panel_id = int(active.panel_message_id)
        self.assertIsNotNone(panel_id, "у медиагруппы должна быть панель")
        panel_sends_before = _panel_sends(h)
        self.assertEqual(len(panel_sends_before), 1, "панель должна быть одна")
        published = _stored(h, draft.id).publication_text
        posts_before = len(h.posts())
        posts_snapshot = h.posts()

        await _open_description(h, active.id)
        created_before = _created_count(h)
        await h.text(LONG_CAPTION)

        # Причина и клавиатура — в ТОЙ ЖЕ панели (`editMessageText`).
        panel_edits = [
            call
            for call in h.calls
            if is_panel_record(call) and call["type"] == "edit_message"
        ]
        self.assertTrue(panel_edits, "панель не перерисована")
        self.assertTrue(
            all(int(call["message_id"]) == panel_id for call in panel_edits),
            "правится не наша панель",
        )
        self.assertIn(
            f"лимите {ANNOUNCEMENT_CAPTION_LIMIT}",
            h.bot.content_text(h.chat_id, panel_id) or "",
        )
        rows = _error_keyboard(h, panel_id)
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual(rows[0][0].text, HINT)
        self.assertTrue(is_disabled(rows[0][0]))
        self.assertEqual(rows[1][0].text, CANCEL)
        self.assertTrue(rows[1][0].callback_data.startswith("anq_cancel:"))

        # Публикация и чат не изменились, второй панели/сообщений нет.
        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_text, published, "анонс переиздан")
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(
            stored.delivered_message_ids, tuple(active.message_ids), "адреса изменены"
        )
        self.assertEqual(
            h.posts(), posts_snapshot, "сообщение анонса в чате изменилось"
        )
        self.assertEqual(len(h.posts()), posts_before, "лишняя публикация")
        self.assertEqual(_panel_sends(h), panel_sends_before, "вторая панель")
        self.assertEqual(_created_count(h), created_before, "лишние сообщения")
        self.assertEqual(_only_active(h, draft.id).panel_message_id, panel_id)
        value_row = next(
            row for row in _value_rows(h, draft.id) if row[0] == bricks["f0"]
        )
        self.assertIn(LONG_CAPTION, value_row[2] or "", "правка не сохранена")


# --- E. отмена: только это поле, экран закрыт, повтор безопасен ------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class LengthErrorCancelTests(unittest.TestCase):
    """§3: «Отменить» возвращает прежнее значение и не трогает остальное."""

    @scenario_test
    async def test_e_cancel_restores_only_edited_field(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)
        published = _stored(h, draft.id).publication_text
        second_before = next(
            row for row in _value_rows(h, draft.id) if row[0] == bricks["f1"]
        )

        await _open_description(h, active.id)
        await h.text(LONG_CAPTION)
        rows = _error_keyboard(h, message_id)
        cancel_data = rows[1][0].callback_data

        created_before = _created_count(h)
        posts_before = len(h.posts())
        await h.callback(cancel_data)

        # Значение поля вернулось к последнему опубликованному.
        value_row = next(
            row for row in _value_rows(h, draft.id) if row[0] == bricks["f0"]
        )
        self.assertEqual(value_row[1], "valid")
        self.assertIn(OLD_DESCRIPTION, value_row[2] or "")
        self.assertNotIn(
            LONG_CAPTION, value_row[2] or "", "незавершённая правка осталась"
        )
        # Другие поля не тронуты.
        second_after = next(
            row for row in _value_rows(h, draft.id) if row[0] == bricks["f1"]
        )
        self.assertEqual(second_after, second_before, "откатилось другое поле")
        # Публикации не было: доставленная версия та же.
        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_text, published, "анонс переиздан")
        self.assertEqual(stored.delivered_message_ids, (message_id,))
        self.assertEqual(h.bot.content_text(h.chat_id, message_id), published)
        self.assertEqual(len(h.posts()), posts_before, "лишняя публикация")
        self.assertEqual(_created_count(h), created_before, "лишние сообщения")
        # Экран ошибки закрыт: на управлении снова список полей.
        self.assertTrue(_field_buttons(h), f"списка нет: {h.live_buttons()}")
        self.assertEqual(h.live_callbacks("anq_cancel"), [], "экран ошибки остался")

        # Повторное нажатие той же кнопки: безопасный stale-ответ, без откатов.
        await h.callback(cancel_data)
        self.assertIn(
            controls.STALE_INSTANCE_TEXT,
            h.answer_texts(),
            f"нет ответа на устаревшую кнопку: {h.answer_texts()}",
        )
        self.assertEqual(
            _stored(h, draft.id).publication_text, published, "повтор изменил анонс"
        )


# --- гонка: поздняя отмена после успешного сохранения ----------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class LateCancelRaceTests(unittest.TestCase):
    """§3: поздний callback старой отмены не откатывает новое значение."""

    @scenario_test
    async def test_late_cancel_after_success_keeps_new_value(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)

        await _open_description(h, active.id)
        await h.text(LONG_CAPTION)
        stale_cancel = _error_keyboard(h, message_id)[1][0].callback_data

        # Пока экран ошибки висел, автор прислал подходящий текст — правка
        # применилась штатно.
        await h.text(FIXED_DESCRIPTION)
        fixed = _stored(h, draft.id)
        self.assertIn(FIXED_DESCRIPTION, fixed.publication_text or "")
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), fixed.publication_text
        )

        # Позднее нажатие старой «Отменить»: ревизия/отметка не совпадают.
        await h.callback(stale_cancel)
        after = _stored(h, draft.id)
        self.assertEqual(
            after.publication_text, fixed.publication_text, "новое значение откатили"
        )
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), after.publication_text
        )
        value_row = next(
            row for row in _value_rows(h, draft.id) if row[0] == bricks["f0"]
        )
        self.assertIn(FIXED_DESCRIPTION, value_row[2] or "")
        self.assertEqual(after.delivered_message_ids, fixed.delivered_message_ids)


# --- лимиты: считаются по видимому тексту после разметки ------------------


class LengthLimitTests(unittest.TestCase):
    """§3: 4096 для текста и 1024 для подписи — после снятия разметки."""

    def test_markup_does_not_count_as_characters(self) -> None:
        exact = "<b>" + "а" * ANNOUNCEMENT_TEXT_LIMIT + "</b>"
        over = "<b>" + "а" * (ANNOUNCEMENT_TEXT_LIMIT + 1) + "</b>"
        self.assertFalse(
            check_announcement_length(exact, has_photos=False).over, "лимит 4096"
        )
        too_long = check_announcement_length(over, has_photos=False)
        self.assertTrue(too_long.over)
        self.assertEqual(too_long.limit, ANNOUNCEMENT_TEXT_LIMIT)

    def test_caption_limit_is_1024(self) -> None:
        exact = "<i>" + "б" * ANNOUNCEMENT_CAPTION_LIMIT + "</i>"
        over = "<i>" + "б" * (ANNOUNCEMENT_CAPTION_LIMIT + 1) + "</i>"
        self.assertFalse(check_announcement_length(exact, has_photos=True).over)
        too_long = check_announcement_length(over, has_photos=True)
        self.assertTrue(too_long.over)
        self.assertEqual(too_long.limit, ANNOUNCEMENT_CAPTION_LIMIT)

    def test_entities_are_counted_once(self) -> None:
        # `&amp;` — один видимый символ, а не пять: лимит совпадает с тем,
        # что реально покажет Telegram.
        html = "&amp;" * ANNOUNCEMENT_TEXT_LIMIT
        self.assertFalse(check_announcement_length(html, has_photos=False).over)


def _value_rows(h, draft_id: int) -> list[tuple[int, str, str | None]]:
    """Все строки `announcement_values` черновика (brick_id, state, value_json)."""
    from sqlalchemy import text

    with h.session_factory() as session:
        rows = session.execute(
            text(
                "SELECT brick_id, state, value_json FROM announcement_values "
                "WHERE draft_id = :draft_id ORDER BY brick_id"
            ),
            {"draft_id": int(draft_id)},
        ).all()
    return [(int(row[0]), str(row[1]), row[2]) for row in rows]
