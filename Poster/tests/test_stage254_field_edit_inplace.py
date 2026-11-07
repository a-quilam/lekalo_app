# tests/test_stage254_field_edit_inplace.py
"""
Poster 2.5.4 §1 — ВВОД значения текстового поля живёт на самом анонсе.

Раньше выбор поля в списке «📝 Текстовые поля» открывал отдельное служебное
сообщение-вопрос: у доставленного анонса появлялось второе место
управления, а после сохранения оно закрывалось. Теперь ввод — это та же
клавиатура, что и список:

- 0–1 фото — `editMessageReplyMarkup` сообщения анонса (текст/подпись не
  трогаются);
- 2+ фото — `editMessageText` ЕДИНСТВЕННОЙ панели медиагруппы.

Нового сообщения не появляется ни в одном шаге, значение сохраняется общим
маршрутом 2.5.3 (`_apply_immediately`/`_apply_in_place_change`), после чего
клавиатура возвращается к списку полей с актуальными значениями.

Тесты идут через настоящее приложение, FakeBot и временную SQLite
(`tests/integration_harness.py`): реальная `post_bot.db`, `.env` и сеть
Telegram не используются.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage254_field_edit_inplace -v
"""

from __future__ import annotations

import asyncio
import unittest

from utils.ui_texts import tr

try:
    import announcement_controls as controls
    from handlers import announcement_field_input as field_input
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        inline_buttons,
        is_panel_record,
        scenario_test,
    )
    from tests.test_stage25_delivery_instances import (
        _markup_calls,
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
    from tests.test_stage253_text_fields_e2 import _restart_with_new_harness

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


PHOTOS_TITLE = tr("wizard.photos.title")
# Типы вызовов, создающих НОВОЕ сообщение в чате.
CREATE_TYPES = frozenset({"message", "photo", "media_group", "rich_message"})
# Поля анонса: короткий текст, время, дата и одиночный выбор.
SPECS: list[tuple[str, str, str, dict[str, object]]] = [
    ("title", "Название", "short_text", {}),
    ("clock", "Время", "time", {}),
    ("day", "Дата", "date", {}),
    ("pick", "Формат", "choice", {"choices": ["Очно", "Онлайн"]}),
]
CONTENT = "[title]\n[clock]\n[day]\n[pick]"


# --- помощники -------------------------------------------------------------


def _created_count(h) -> int:
    """Сколько вызовов создали сообщения бота (без правок и удалений)."""
    return sum(1 for call in h.calls if call["type"] in CREATE_TYPES)


def _input_callbacks(h, prefix: str) -> list[str]:
    """Живые `anq_`-callback'и клавиатуры ввода (префикс действия)."""
    return [
        data for data in h.live_callbacks("anq_") if data.startswith(f"anq_{prefix}")
    ]


def _field_buttons(h) -> list[tuple[str, str]]:
    """Живые кнопки полей списка (подпись, `anf_sel:` callback)."""
    return [
        (label, data)
        for label, data in h.live_buttons()
        if data and data.startswith("anf_sel:")
    ]


def _input_markup(h, message_id: int):
    """
    Клавиатура КОНКРЕТНОГО сообщения (панели или анонса).

    Ввод правит либо только клавиатуру анонса (`editMessageReplyMarkup`),
    либо текст и клавиатуру единственной панели (`editMessageText`), поэтому
    берётся последний вызов любого вида с `reply_markup` для этого сообщения.
    """
    latest = None
    for call in h.calls:
        if call.get("message_id") is None:
            continue
        if int(call["message_id"]) != int(message_id):
            continue
        if call.get("reply_markup") is not None:
            latest = call.get("reply_markup")
    return latest


def _input_buttons(h, message_id: int) -> list:
    return list(inline_buttons(_input_markup(h, message_id)))


def _hint_buttons(h, message_id: int):
    """Кнопки-подсказки «⏳ Ожидаю …» (неактивные) в клавиатуре сообщения."""
    from utils.inline_buttons import is_disabled

    markup = _input_markup(h, message_id)
    if markup is None or not hasattr(markup, "inline_keyboard"):
        return []
    return [
        button
        for row in markup.inline_keyboard or []
        for button in row
        if is_disabled(button)
    ]


async def _answer_questions(h, fields: int = len(SPECS)) -> None:
    """
    Заполняет вопросы шаблона, пока не откроется шаг фото.

    Текст поля задаётся непустым: у фото-анонса подпись должна быть непустой,
    иначе FakeBot (как и Telegram) считает сообщение без caption и правка
    текста на месте проверяется не тем путём.
    """
    for _ in range(fields + 6):
        if PHOTOS_TITLE in h.screen_text():
            return
        buttons = [data for _label, data in h.screen_buttons()]
        preset = next((data for data in buttons if data.startswith("aw_timeset")), None)
        if preset is not None:
            await h.callback(preset)
            continue
        choice = next((data for data in buttons if data.startswith("aw_choice:")), None)
        if choice is not None:
            await h.callback(choice)
            continue
        skip = next((data for data in buttons if data.startswith("aw_qskip")), None)
        if any(data.startswith("aw_calday") for data in buttons) and skip is not None:
            await h.callback(skip)
            continue
        await h.text("Значение")
    raise AssertionError(f"Не шаг фото: {h.screen_text()[:200]!r}")


async def _delivered(h, *, photos: int = 1):
    """Личный шаблон из `SPECS` с `photos` фото, доставленный в чат."""
    place_id, _alias = _seed_place(h)
    template_id, _revision, bricks = _seed_personal_template(
        h, name="Шаблон 2.5.4", specs=SPECS, content=CONTENT
    )
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    await _answer_questions(h)
    if photos == 0:
        await h.callback(_controls_callback(h, "aw_photo_none"))
    elif photos == 1:
        await h.photo("photo-0")
    else:
        for index in range(photos):
            await h.photo(f"photo-{index}", media_group_id="album")
        await asyncio.sleep(0.4)
    draft = _latest_draft(h)
    if _stored(h, draft.id).publication_status != "sent":
        raise AssertionError("анонс не доставлен")
    return draft, bricks


def _controls_callback(h, prefix: str) -> str:
    for _label, data in h.screen_buttons():
        if data and data.startswith(prefix):
            return data
    raise AssertionError(f"нет кнопки {prefix!r}: {h.screen_buttons()}")


def _field_callback(h, brick_id: int) -> str:
    """`anf_sel` конкретного поля из живого списка."""
    suffix = f":{int(brick_id)}"
    for _label, data in _field_buttons(h):
        if data.endswith(suffix):
            return data
    raise AssertionError(f"нет поля #{brick_id}: {h.live_buttons()}")


async def _open_list(h, instance_id: int) -> None:
    await h.callback(controls.panel_callback(controls.ACTION_DATA, instance_id))


async def _open_input(h, instance_id: int, brick_id: int) -> str:
    """Открывает ввод поля и возвращает `callback_data` кнопки поля."""
    if not _field_buttons(h):
        await _open_list(h, instance_id)
    data = _field_callback(h, brick_id)
    await h.callback(data)
    return data


def _announcement_message_id(h, instance) -> int:
    return int(instance.message_ids[0])


# --- (а)–(г): 0–1 фото — ввод на клавиатуре анонса ------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FieldInputOnAnnouncementTests(unittest.TestCase):
    """§1: ввод поля — та же клавиатура анонса, без сообщения-вопроса."""

    @scenario_test
    async def test_a_field_choice_changes_markup_only(self, h) -> None:
        """(а) выбор поля не создаёт сообщений и меняет ту же клавиатуру."""
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_message_id(h, active)

        await _open_list(h, active.id)
        created_before = _created_count(h)
        calls_before = len(h.calls)

        await h.callback(_field_callback(h, bricks["title"]))

        self.assertEqual(
            _created_count(h), created_before, "выбор поля не создаёт сообщений"
        )
        new_calls = h.calls[calls_before:]
        self.assertNotIn("message", {call["type"] for call in new_calls})
        self.assertIsNone(h.screen_message_id(), "отдельного экрана-вопроса нет")

        # Клавиатура ИМЕННО сообщения анонса заменена на клавиатуру ввода.
        markup_calls = _markup_calls(h, message_id)
        self.assertTrue(markup_calls, "клавиатура анонса не обновлена")
        buttons = _input_buttons(h, message_id)
        self.assertTrue(buttons, "клавиатура ввода не показана")
        self.assertTrue(
            any(data and data.startswith("anq_") for _label, data in buttons),
            f"нет кнопок ввода: {buttons}",
        )
        # Подпись/текст анонса не тронуты: правилась только клавиатура.
        stored = _stored(h, draft.id)
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), stored.publication_text
        )
        self.assertEqual(_only_active(h, draft.id).message_ids, (message_id,))

    @scenario_test
    async def test_b_hint_is_disabled_button(self, h) -> None:
        """(б) «⏳ Ожидаю <поле>» — неактивная кнопка без callback_data."""
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_message_id(h, active)
        await _open_input(h, active.id, bricks["title"])

        hints = _hint_buttons(h, message_id)
        self.assertEqual(len(hints), 1, f"подсказка не найдена: {h.live_buttons()}")
        hint = hints[0]
        self.assertTrue(hint.text.startswith("⏳ Ожидаю"), hint.text)
        self.assertIn("Название", hint.text)
        self.assertIsNone(hint.callback_data, "у подсказки не должно быть callback")

        # Последняя строка — одна кнопка «Отменить» в стиле `danger`.
        rows = _input_markup(h, message_id).inline_keyboard
        tail = rows[-1]
        self.assertEqual(len(tail), 1, tail)
        self.assertTrue(tail[0].callback_data.startswith("anq_abort:"), tail)
        self.assertEqual(tail[0].text, "Отменить")
        self.assertEqual(tail[0].style, "danger")

    @scenario_test
    async def test_c_time_preset_saved_in_place_and_back_to_list(self, h) -> None:
        """(в) «18:30» сохраняет значение на месте и возвращает список полей."""
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_message_id(h, active)

        await _open_input(h, active.id, bricks["clock"])
        presets = [
            data
            for _label, data in _input_buttons(h, message_id)
            if data and data.startswith("anq_time:")
        ]
        self.assertEqual(len(presets), 3, presets)
        target = next(data for data in presets if data.endswith(":18:30"))

        created_before = _created_count(h)
        await h.callback(target)

        self.assertEqual(_created_count(h), created_before, "сообщений не прибавилось")
        stored = _stored(h, draft.id)
        self.assertIn("18:30", stored.publication_text)
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), stored.publication_text
        )
        self.assertEqual(_only_active(h, draft.id).message_ids, (message_id,))
        # Вернулись к списку полей на том же сообщении.
        self.assertTrue(_field_buttons(h), f"списка нет: {h.live_buttons()}")
        self.assertIsNone(h.screen_message_id(), "служебного экрана нет")
        # Список полей показывает актуальное состояние: «Время — заполнено».
        labels = [label for label, _d in _field_buttons(h)]
        self.assertTrue(any("Время — заполнено" in label for label in labels), labels)

    @scenario_test
    async def test_d_manual_text_saved_in_place(self, h) -> None:
        """(г) ручной текст сохраняется в поле и правит анонс на месте."""
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_message_id(h, active)
        await _open_input(h, active.id, bricks["title"])

        await h.text("Новое название")

        stored = _stored(h, draft.id)
        self.assertIn("Новое название", stored.publication_text)
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), stored.publication_text
        )
        self.assertEqual(_only_active(h, draft.id).message_ids, (message_id,))
        self.assertTrue(_field_buttons(h), "список полей не вернулся")
        self.assertIsNone(h.screen_message_id())

    @scenario_test
    async def test_e_cancel_keeps_previous_value(self, h) -> None:
        """(е) «Отменить» не меняет значение и ведёт к списку полей."""
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_message_id(h, active)
        await _open_input(h, active.id, bricks["title"])
        before = _stored(h, draft.id).publication_text
        revision_before = _stored(h, draft.id).revision
        calls_before = len(h.calls)

        cancel = _input_callbacks(h, "abort")[0]
        await h.callback(cancel)

        self.assertTrue(_field_buttons(h), "«Отменить» не вернул список полей")
        after = _stored(h, draft.id)
        self.assertEqual(after.publication_text, before, "значение изменилось")
        self.assertEqual(after.revision, revision_before)
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), before, "анонс переписан"
        )
        # «Отменить» не отправляет текстовых сообщений: только правка
        # клавиатуры существующего управления.
        new_calls = h.calls[calls_before:]
        self.assertNotIn(
            "message",
            {call["type"] for call in new_calls},
            "«Отменить» не создаёт сообщений",
        )

    @scenario_test
    async def test_e3_same_value_twice_is_not_an_error(self, h) -> None:
        """(в) повторное сохранение того же значения — не ошибка ввода."""
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_message_id(h, active)

        for attempt in range(2):
            await _open_input(h, active.id, bricks["clock"])
            presets = [
                data
                for _label, data in _input_buttons(h, message_id)
                if data and data.startswith("anq_time:")
            ]
            target = next(data for data in presets if data.endswith(":18:30"))
            answers_before = len(h.answer_texts())
            replies_before = len(h.reply_texts())
            await h.callback(target)

            stored = _stored(h, draft.id)
            self.assertIn("18:30", stored.publication_text, f"попытка {attempt + 1}")
            # Telegram отвечает «message is not modified», когда содержимое уже
            # актуально: это успех, поэтому ошибки быть не должно.
            new_answers = h.answer_texts()[answers_before:]
            self.assertFalse(
                [text for text in new_answers if "не удалось" in text.lower()],
                f"сохранение показано ошибкой: {new_answers}",
            )
            self.assertEqual(
                len(h.reply_texts()),
                replies_before,
                "ошибка пришла ответом на сообщение",
            )
            self.assertTrue(_field_buttons(h), "список полей не вернулся")
            self.assertIsNone(h.screen_message_id())
        restricted = _stored(h, draft.id)
        self.assertEqual(restricted.publication_text, stored.publication_text)
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), stored.publication_text
        )
        self.assertEqual(_only_active(h, draft.id).message_ids, (message_id,))

    @scenario_test
    async def test_e4_not_modified_is_success(self, h) -> None:
        """(в) «message is not modified» от Telegram — не отказ ввода."""
        from telegram.error import BadRequest

        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_message_id(h, active)
        await _open_list(h, active.id)

        # Telegram уже показывает эту клавиатуру: Bot API отвечает ошибкой,
        # хотя менять нечего. Это успех, а не «ввод не открылся».
        h.bot.errors_by_type["edit_message"] = BadRequest("message is not modified")
        try:
            await h.callback(_field_callback(h, bricks["title"]))
        finally:
            h.bot.errors_by_type.pop("edit_message", None)

        self.assertTrue(h.live_callbacks("anq_"), "ввод сбросился в список полей")
        self.assertIsNone(h.screen_message_id(), "появился служебный экран")
        self.assertEqual(_only_active(h, draft.id).message_ids, (message_id,))

    @scenario_test
    async def test_e5_save_not_modified_caption_is_success(self, h) -> None:
        """(в) правка анонса «не изменилась» — значение всё равно сохранено."""
        from telegram.error import BadRequest

        await h.text("/start")
        draft, bricks = await _delivered(h, photos=0)
        active = _only_active(h, draft.id)
        message_id = _announcement_message_id(h, active)
        await _open_input(h, active.id, bricks["clock"])

        presets = [
            data
            for _label, data in _input_buttons(h, message_id)
            if data and data.startswith("anq_time:")
        ]
        target = next(data for data in presets if data.endswith(":18:30"))
        answers_before = len(h.answer_texts())
        replies_before = len(h.reply_texts())
        # Telegram отвечает «message is not modified» на правку текста: это
        # успех (содержимое уже актуально), а не сбой сохранения.
        h.bot.errors_by_type["edit_message"] = BadRequest("message is not modified")
        try:
            await h.callback(target)
        finally:
            h.bot.errors_by_type.pop("edit_message", None)

        stored = _stored(h, draft.id)
        self.assertIn("18:30", stored.publication_text, "значение не сохранено")
        self.assertEqual(_only_active(h, draft.id).message_ids, (message_id,))
        self.assertFalse(
            [
                text
                for text in h.answer_texts()[answers_before:]
                if "не удалось" in text.lower()
            ],
            "сохранение показано ошибкой",
        )
        self.assertEqual(len(h.reply_texts()), replies_before, "ошибка ввода")
        self.assertTrue(_field_buttons(h), "список полей не вернулся")
        self.assertIsNone(h.screen_message_id())

    @scenario_test
    async def test_e2_calendar_day_saved_in_place(self, h) -> None:
        """(в/е) день календаря сохраняется тем же маршрутом, без вопросов."""
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_message_id(h, active)
        await _open_input(h, active.id, bricks["day"])

        days = [
            data
            for _label, data in _input_buttons(h, message_id)
            if data and data.startswith("anq_day:")
        ]
        self.assertTrue(days, f"календаря нет: {_input_buttons(h, message_id)}")
        created_before = _created_count(h)
        await h.callback(days[0])

        self.assertEqual(_created_count(h), created_before, "сообщений не прибавилось")
        stored = _stored(h, draft.id)
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), stored.publication_text
        )
        self.assertTrue(_field_buttons(h), "список полей не вернулся")

    @scenario_test
    async def test_g_choice_single_saved_in_place(self, h) -> None:
        """(г) готовый вариант выбора сохраняется на месте."""
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_message_id(h, active)
        await _open_input(h, active.id, bricks["pick"])

        options = [
            data
            for _label, data in _input_buttons(h, message_id)
            if data and data.startswith("anq_choice:")
        ]
        self.assertEqual(len(options), 2, options)
        await h.callback(options[1])

        stored = _stored(h, draft.id)
        self.assertIn("Онлайн", stored.publication_text)
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), stored.publication_text
        )
        self.assertTrue(_field_buttons(h), "список полей не вернулся")


# --- (ж): рестарт ----------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FieldInputRestartTests(unittest.TestCase):
    """§1 п.7: ввод продолжает работать после рестарта службы."""

    @scenario_test
    async def test_f_input_survives_restart(self, h) -> None:
        """(ж) после рестарта текст попадает в то же поле, без «зависаний»."""
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_message_id(h, active)
        await _open_input(h, active.id, bricks["title"])

        fresh = await _restart_with_new_harness(h)
        try:
            # Состояние диалога потеряно, снимок ввода — в БД.
            self.assertIsNone(fresh.user_data().get("announcement_ui"))
            await fresh.text("После рестарта")

            stored = _stored(fresh, draft.id)
            self.assertIn("После рестарта", stored.publication_text)
            self.assertEqual(
                fresh.bot.content_text(fresh.chat_id, message_id),
                stored.publication_text,
            )
            self.assertEqual(_only_active(fresh, draft.id).message_ids, (message_id,))
            self.assertTrue(_field_buttons(fresh), "список полей не вернулся")
        finally:
            await fresh.close()

    @scenario_test
    async def test_f2_restart_without_field_does_not_hang(self, h) -> None:
        """(ж) без сохранённого ввода рестарт даёт обычную подсказку меню."""
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_message_id(h, active)
        await _open_input(h, active.id, bricks["title"])
        # «Отменить» снимает режим ввода: снимка «ждём текст» быть не должно.
        await h.callback(_input_callbacks(h, "abort")[0])

        fresh = await _restart_with_new_harness(h)
        try:
            before = _stored(fresh, draft.id).publication_text
            await fresh.text("Никуда не должно попасть")
            stored = _stored(fresh, draft.id)
            self.assertEqual(stored.publication_text, before, "значение записалось")
            self.assertEqual(fresh.bot.content_text(fresh.chat_id, message_id), before)
        finally:
            await fresh.close()

    @scenario_test
    async def test_f3_resume_rejects_foreign_chat(self, h) -> None:
        """(ж) снимок ввода помнит чат: из другого чата значение не пишется."""
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_message_id(h, active)
        await _open_input(h, active.id, bricks["title"])

        fresh = await _restart_with_new_harness(h)
        try:
            before = _stored(fresh, draft.id).publication_text
            # Пересланная клавиатура: чат другой, анонс живёт в исходном.
            await fresh.text("Из чужого чата", chat_id=fresh.chat_id + 1)

            self.assertEqual(
                _stored(fresh, draft.id).publication_text,
                before,
                "значение записалось из чужого чата",
            )
            self.assertEqual(fresh.bot.content_text(fresh.chat_id, message_id), before)
        finally:
            await fresh.close()


# --- (з): медиагруппа ------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FieldInputMediaGroupTests(unittest.TestCase):
    """§1: у 2+ фото ввод — текст ЕДИНСТВЕННОЙ панели, второй не появляется."""

    @scenario_test
    async def test_g_media_group_uses_single_panel(self, h) -> None:
        """(з) ввод и возврат идут через ту же панель медиагруппы."""
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=2)
        active = _only_active(h, draft.id)
        panel_id = int(active.panel_message_id)
        panel_sends_before = _panel_sends(h)
        self.assertEqual(len(panel_sends_before), 1, "панель должна быть одна")

        await _open_list(h, active.id)
        await h.callback(_field_callback(h, bricks["clock"]))

        # Вторая панель не создана, правится та же.
        self.assertEqual(_panel_sends(h), panel_sends_before, "вторая панель")
        self.assertEqual(_only_active(h, draft.id).panel_message_id, panel_id)
        panel_calls = [
            call
            for call in h.calls
            if is_panel_record(call) and call["type"] == "edit_message"
        ]
        self.assertTrue(panel_calls, "панель не перерисована")
        self.assertTrue(
            all(int(call["message_id"]) == panel_id for call in panel_calls),
            "правится не наша панель",
        )
        buttons = _input_buttons(h, panel_id)
        self.assertTrue(
            any(data and data.startswith("anq_") for _label, data in buttons),
            f"клавиатура ввода не в панели: {buttons}",
        )

        # Сохранение значения — по-прежнему на месте: панель остаётся одна.
        presets = [
            data for _label, data in buttons if data and data.startswith("anq_time:")
        ]
        created_before = _created_count(h)
        await h.callback(next(data for data in presets if data.endswith(":18:00")))

        self.assertEqual(_created_count(h), created_before, "сообщений не прибавилось")
        self.assertEqual(_panel_sends(h), panel_sends_before, "вторая панель")
        self.assertEqual(_only_active(h, draft.id).panel_message_id, panel_id)
        stored = _stored(h, draft.id)
        self.assertIn("18:00", stored.publication_text)
        self.assertTrue(_field_buttons(h), "список полей не вернулся в панель")

    @scenario_test
    async def test_g2_media_group_manual_text_without_new_messages(self, h) -> None:
        """(з) ручной ввод у медиагруппы не создаёт сообщений и второй панели."""
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=2)
        active = _only_active(h, draft.id)
        panel_id = int(active.panel_message_id)
        await _open_list(h, active.id)
        await h.callback(_field_callback(h, bricks["title"]))

        created_before = _created_count(h)
        await h.text("Альбомное название")
        self.assertEqual(_created_count(h), created_before, "сообщений не прибавилось")
        self.assertEqual(_only_active(h, draft.id).panel_message_id, panel_id)
        self.assertEqual(
            h.bot.content_text(h.chat_id, int(active.message_ids[0])),
            _stored(h, draft.id).publication_text,
        )
        self.assertTrue(_field_buttons(h))


# --- протокол callback -----------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FieldInputProtocolTests(unittest.TestCase):
    """Адрес `anq_` несёт только id экземпляра и кирпичика (§32)."""

    def test_callback_roundtrip(self) -> None:
        data = controls.field_input_callback(
            controls.FIELD_INPUT_ACTION_TIME, 12, 34, "18:30"
        )
        self.assertEqual(data, "anq_time:12:34:18:30")
        self.assertEqual(
            controls.parse_field_input_callback(data), ("time", 12, 34, ("18", "30"))
        )
        self.assertIsNotNone(
            controls.FIELD_INPUT_CALLBACK_PATTERN
            and __import__("re").match(controls.FIELD_INPUT_CALLBACK_PATTERN, data)
        )
        for bad in (
            "anq_time:12",
            "anq_back:0:34",
            "anq_unknown:12:34",
            "anf_sel:12:34",
            "",
        ):
            self.assertIsNone(controls.parse_field_input_callback(bad), bad)

    def test_input_keyboard_rows(self) -> None:
        """Клавиатура ввода: подсказка, варианты, одна «Отменить»."""
        from dataclasses import replace

        from announcement_store import AnnouncementFieldState
        from brick_types import BrickValueStatus

        field = AnnouncementFieldState(
            brick_id=7,
            code="clock",
            title="Время",
            value_type="time",
            status=BrickValueStatus.VALID,
            raw_value="10:00",
            rules={},
        )
        keyboard = field_input.input_keyboard(field, 3)
        rows = keyboard.inline_keyboard
        self.assertEqual(len(rows[0]), 1, "подсказка отдельной строкой")
        self.assertIsNone(rows[0][0].callback_data)
        self.assertEqual(len(rows[1]), 3, "три быстрых времени")
        self.assertEqual(len(rows[-1]), 1, "одна кнопка навигации")
        self.assertEqual(rows[-1][0].text, "Отменить")
        self.assertEqual(rows[-1][0].callback_data, "anq_abort:3:7")
        self.assertEqual(rows[-1][0].style, "danger")
        self.assertEqual(field_input.hint_label(field), "⏳ Ожидаю Время")
        self.assertEqual(replace(field, title="Время").code, "clock")


if __name__ == "__main__":
    unittest.main()
