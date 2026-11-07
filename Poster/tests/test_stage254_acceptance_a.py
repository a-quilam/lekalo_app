# tests/test_stage254_acceptance_a.py
"""
Poster 2.5.4 §1 + приёмка A §9 — независимая проверка ПОЛЬЗОВАТЕЛЬСКОГО
контракта «Редактирование полей готового анонса без дополнительных служебных
сообщений».

Проверяется требование, а не реализация: сценарии идут полным путём автора
через настоящее приложение (`bot.build_application`), FakeBot и временную
SQLite (`tests/integration_harness.py`). Реальная `post_bot.db`, `.env` и сеть
Telegram не используются.

Контракт (по пунктам задания):

1. Готовый анонс (1 фото) → «📝 Текстовые поля» → выбор «Время начала»:
   нового `sendMessage` нет; у того же `message_id` появляется клавиатура
   `⏳ Ожидаю <поле>` (неактивная: без `callback_data`, сериализация
   `disabled`), строка `18:00 | 18:30 | 19:00`, одна кнопка «Отменить».
2. `18:30` → время изменено в анонсе на месте, `message_id` сохранён,
   клавиатура вернулась к списку полей с АКТУАЛЬНЫМИ значениями, сообщения
   об успехе нет.
3. Ручной ввод времени текстом («20:15») — тот же результат.
4. Рестарт (новый harness-контекст, состояния диалога пусты) — управление
   продолжает работать: список открывается, поле выбирается, значение
   сохраняется.
5. Текстовое поле (описание): подсказка `⏳ Ожидаю …`, одна кнопка
   «Отменить», отправка текста применяется сразу, без
   «Теперь введите описание».
6. «Отменить» → список полей; прежнее значение сохранено; медиагруппа
   (2+ фото) — управление через её ЕДИНСТВЕННУЮ панель, второй панели и
   отдельных `sendMessage` нет.
7. Нет конкурирующих клавиатур: после каждого шага в чате ровно одна
   актуальная управляющая клавиатура у анонса (или его панели).
8. Дата и контакт — та же схема; дата открывается кнопками календаря на
   клавиатуре анонса, а не новым сообщением.
9. Поля площадки (название, город, адрес, ссылка) в списке полей редактора
   ОТСУТСТВУЮТ; поддельный callback на них не меняет данные.

Дополнительно (обязательные негативные сценарии): неверный ввод времени не
теряет значение и не ломает клавиатуру; повторное нажатие пресета — не
ошибка; сбой штатной правки анонса («Message to edit not found») не теряет
значение и оставляет ровно одно место управления.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage254_acceptance_a -v
"""

from __future__ import annotations

import asyncio
import re
import unittest

from utils.ui_texts import tr

try:
    from telegram.error import BadRequest

    from announcement_store import FIELD_STATUS_KNOWN, build_announcement_fields
    from brick_types import SYSTEM_BRICKS_BY_CODE
    from template_store import get_standard_template, update_template_content
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.test_stage25_delivery_instances import (
        _only_active,
        _panel_sends,
        _stored,
    )
    from tests.test_stage27b_announcement_ui import (
        _callback,
        _latest_draft,
        _seed_place,
        _start_to_template,
    )
    from tests.test_stage253_acceptance_final import (
        _draft_row,
        _place_row,
        _value_rows,
    )
    from tests.test_stage253_text_fields_e2 import _restart_with_new_harness
    from utils.inline_buttons import is_disabled

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


# Подписи и тексты — требование владельца, а не значения из каталога:
# тест обязан заметить подмену текста в конфигурации.
PHOTOS_TITLE = tr("wizard.photos.title")
WAIT = "⏳ Ожидаю"
CANCEL = "Отменить"
PRESETS = ("18:00", "18:30", "19:00")

# Типы вызовов, создающих НОВОЕ сообщение в чате.
CREATE_TYPES = frozenset({"message", "photo", "media_group", "rich_message"})

# Системные кирпичики стандартного шаблона площадки.
NAME_ID = SYSTEM_BRICKS_BY_CODE["название"].id
DATE_ID = SYSTEM_BRICKS_BY_CODE["дата"].id
START_ID = SYSTEM_BRICKS_BY_CODE["время_начала"].id
DESC_ID = SYSTEM_BRICKS_BY_CODE["описание"].id
CONTACT_ID = SYSTEM_BRICKS_BY_CODE["контакт"].id

# Поля площадки: значения из справочника, править их в анонсе нельзя (§9).
PLACE_CODES = ("место", "адрес", "ссылка_места", "город")
PLACE_IDS = tuple(SYSTEM_BRICKS_BY_CODE[code].id for code in PLACE_CODES)
PLACE_TITLES = tuple(SYSTEM_BRICKS_BY_CODE[code].title for code in PLACE_CODES)

# Текстовые поля, которые обязаны остаться в редакторе.
EDITABLE_TITLES = (
    "Название мероприятия",
    "Дата",
    "Время начала",
    "Описание",
    "Контакт",
)


# --- помощники -------------------------------------------------------------


def _created_count(h) -> int:
    """Сколько вызовов создали сообщения бота (без правок и удалений)."""
    return sum(1 for call in h.calls if call["type"] in CREATE_TYPES)


def _created_since(h, start: int) -> list[dict]:
    return [call for call in h.calls[start:] if call["type"] in CREATE_TYPES]


def _messages_since(h, start: int) -> list[str]:
    """Тексты НОВЫХ сообщений бота (sendMessage), созданных с шага `start`."""
    out: list[str] = []
    for call in h.calls[start:]:
        if call["type"] != "message":
            continue
        text = call.get("text")
        if isinstance(text, str) and text:
            out.append(text)
    return out


def _answer_texts_since(h, start: int) -> list[str]:
    """Непустые тексты ответов на callback, созданных с шага `start`."""
    return [
        call["text"]
        for call in h.calls[start:]
        if call["type"] == "callback_answer" and call.get("text")
    ]


def _markup_for(h, message_id: int):
    """
    Клавиатура КОНКРЕТНОГО сообщения на текущий момент.

    У сообщения анонса (0–1 фото) это может быть `editMessageReplyMarkup`,
    у панели медиагруппы — `editMessageText`; берётся последний вызов с
    `reply_markup` для этого `message_id`.
    """
    latest = None
    for call in h.calls:
        if call.get("message_id") is None:
            continue
        if int(call["message_id"]) != int(message_id):
            continue
        if call.get("reply_markup") is not None:
            latest = call["reply_markup"]
    return latest


def _markup_signature(h, message_id: int):
    """Подпись клавиатуры из FakeBot: None — у сообщения кнопок сейчас нет."""
    entry = h.bot._message_content.get((int(h.chat_id), int(message_id)))
    return entry[2] if entry is not None else None


def _rows(markup) -> list[list]:
    """Строки inline-клавиатуры (объекты кнопок)."""
    if markup is None or not hasattr(markup, "inline_keyboard"):
        return []
    return [list(row) for row in (markup.inline_keyboard or [])]


def _row_texts(rows: list[list]) -> list[list[str]]:
    return [[button.text for button in row] for row in rows]


def _control_keyboards(h) -> dict[int, list[str]]:
    """
    Живые сообщения, несущие кнопки управления/правки анонса.

    Учитываются `anp_` (управление экземпляром), `anf_` (список полей) и
    `anq_` (ввод значения). Удалённые сообщения и сообщения без клавиатуры
    пропускаются: цель проверки — «в чате одно место управления».
    """
    deleted = {int(mid) for mid in h.deleted_ids()}
    found: dict[int, list[str]] = {}
    for (_chat_id, message_id), (
        _kind,
        _text,
        markup,
    ) in h.bot._message_content.items():
        if int(message_id) in deleted or not markup:
            continue
        callbacks = re.findall(r"an[pfq]_[a-z_]+", str(markup))
        if callbacks:
            found[int(message_id)] = callbacks
    return found


def _assert_one_control(h, expected_message_id: int, where: str) -> None:
    """Ровно одна актуальная управляющая клавиатура — у заданного сообщения."""
    found = _control_keyboards(h)
    if sorted(found) != [int(expected_message_id)]:
        raise AssertionError(
            f"{where}: управляющие клавиатуры {found}, "
            f"ожидалась ровно одна у сообщения {expected_message_id}"
        )


def _field_buttons(h) -> list[tuple[str, str]]:
    """Живые кнопки списка полей (подпись, `anf_sel:` callback)."""
    return [
        (label, data)
        for label, data in h.live_buttons()
        if data and data.startswith("anf_sel:")
    ]


def _field_callback(h, brick_id: int) -> str:
    suffix = f":{int(brick_id)}"
    for _label, data in _field_buttons(h):
        if data.endswith(suffix):
            return data
    raise AssertionError(f"в списке нет поля #{brick_id}: {h.live_buttons()}")


def _field_list_body() -> str:
    """Тело списка полей на панели медиагруппы (заголовок и подсказка)."""
    return f"{tr('wizard.fields.title')}\n\n{tr('wizard.fields.hint')}"


async def _ensure_list(h, instance_id: int) -> None:
    """Открывает список полей из любого текущего состояния управления."""
    if _field_buttons(h):
        return
    input_cancel = h.live_callbacks("anq_abort:")
    if input_cancel:
        await h.callback(input_cancel[0])
        return
    if not h.live_callbacks(f"anp_data:{instance_id}"):
        raise AssertionError(f"нет живого управления: {h.live_buttons()}")
    await h.callback(f"anp_data:{instance_id}")


async def _open_input(h, instance_id: int, brick_id: int) -> str:
    """Открывает ввод поля и возвращает `callback_data` кнопки поля."""
    await _ensure_list(h, instance_id)
    data = _field_callback(h, brick_id)
    await h.callback(data)
    return data


def _input_callbacks(h, action: str) -> list[str]:
    prefix = f"anq_{action}:"
    return [data for data in h.live_callbacks(prefix)]


async def _delivered_standard(h, *, photos: int = 1):
    """
    Стандартный шаблон площадки с добавленным `[город]` → доставка.

    В анонсе одновременно есть все поля площадки (место, ссылка, адрес,
    город) и пользовательские поля (название, дата, время начала, описание,
    контакт) — на них проверяются пункты 1–9 контракта.
    `photos`: 1 — sendPhoto, 2+ — альбом с единственной панелью.
    """
    place_id, _alias = _seed_place(h)
    with h.session_factory() as session:
        standard = get_standard_template(session, DEFAULT_USER_ID, place_id)
        updated = update_template_content(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            template_id=standard.template_id,
            expected_revision=standard.revision,
            content=standard.current_version.content + "\n[город]",
        )
        if not updated.ok:
            raise AssertionError(f"стандарт не обновлён: {updated.message}")
        standard_id = standard.template_id
    await _start_to_template(h, place_id)
    await h.callback(_callback(h, "aw_template", f":{standard_id}"))
    for _ in range(30):
        if PHOTOS_TITLE in h.screen_text():
            break
        skips = [
            data
            for _label, data in h.screen_buttons()
            if data and data.startswith("aw_qskip")
        ]
        if not skips:
            break
        await h.callback(skips[0])
    if PHOTOS_TITLE not in h.screen_text():
        raise AssertionError(f"не шаг фото: {h.screen_text()[:200]!r}")
    if photos == 1:
        await h.photo("photo-0")
    else:
        for index in range(photos):
            await h.photo(f"photo-{index}", media_group_id="album")
        # Альбом закрывается по паузе приёма (как в настоящем Telegram).
        await asyncio.sleep(0.4)
    draft = _latest_draft(h)
    if _stored(h, draft.id).publication_status != "sent":
        raise AssertionError("анонс не доставлен")
    return draft, place_id


def _announcement_id(instance) -> int:
    return int(instance.message_ids[0])


# --- 1–3. Одно фото: время на клавиатуре анонса ----------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TimeFieldInPlaceTests(unittest.TestCase):
    """§1: выбор поля и сохранение значения — на самом анонсе."""

    @scenario_test
    async def test_a_time_input_keyboard_on_announcement(self, h) -> None:
        """(1) выбор «Время начала» правит клавиатуру анонса, а не шлёт сообщение."""
        await h.text("/start")
        draft, _place_id = await _delivered_standard(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_id(active)
        _assert_one_control(h, message_id, "сразу после доставки")

        created = _created_count(h)
        await h.callback(f"anp_data:{active.id}")
        self.assertEqual(
            _created_count(h), created, "«📝 Текстовые поля» создали сообщение"
        )
        self.assertIsNone(h.screen_message_id(), "список полей — отдельный экран")
        self.assertTrue(_field_buttons(h), "список полей не открылся")
        _assert_one_control(h, message_id, "список полей")

        start = len(h.calls)
        await h.callback(_field_callback(h, START_ID))

        # Нового sendMessage нет, служебного экрана-вопроса тоже.
        self.assertEqual(_created_since(h, start), [], "выбор поля создал сообщение")
        self.assertEqual(_created_count(h), created, "выбор поля создал сообщение")
        self.assertIsNone(h.screen_message_id(), "появился экран-вопрос")
        self.assertEqual(
            _only_active(h, draft.id).message_ids,
            (message_id,),
            "адрес анонса изменился",
        )

        markup = _markup_for(h, message_id)
        rows = _rows(markup)
        self.assertEqual(
            _row_texts(rows),
            [
                [WAIT + " 🕒 Время начала"],
                list(PRESETS),
                [CANCEL],
            ],
            "клавиатура ввода отличается от контракта",
        )

        # Строка 1 — НЕАКТИВНАЯ кнопка без callback_data и с `disabled`.
        hint = rows[0][0]
        self.assertIsNone(hint.callback_data, "у подсказки есть callback_data")
        self.assertTrue(is_disabled(hint), "подсказка нажимается")
        self.assertIn(WAIT, hint.text)
        self.assertIn("Время начала", hint.text)
        payload = markup.to_dict()["inline_keyboard"][0][0]
        self.assertIn("disabled", payload, "disabled не сериализуется")
        self.assertNotIn("callback_data", payload, "подсказка сериализуется как кнопка")

        # Строка 2 — ровно три готовых времени, все ведут в ввод этого поля.
        self.assertEqual([button.text for button in rows[1]], list(PRESETS))
        for button in rows[1]:
            self.assertTrue(
                button.callback_data and button.callback_data.startswith("anq_time:"),
                button.callback_data,
            )

        # Строка 3 — одна «Отменить» в стиле `danger` (2.5.5 §1).
        self.assertEqual([button.text for button in rows[2]], [CANCEL])
        self.assertTrue(rows[2][0].callback_data.startswith("anq_abort:"))
        self.assertEqual(rows[2][0].style, "danger")
        _assert_one_control(h, message_id, "клавиатура ввода")

    @scenario_test
    async def test_b_preset_changes_value_in_place_and_returns_list(self, h) -> None:
        """(2) «18:30» → анонс на месте, message_id сохранён, список полей."""
        await h.text("/start")
        draft, _place_id = await _delivered_standard(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_id(active)
        await _open_input(h, active.id, START_ID)

        presets = [button for button in _rows(_markup_for(h, message_id))[1]]
        target = next(button for button in presets if button.text == "18:30")

        start = len(h.calls)
        await h.callback(target.callback_data)

        stored = _stored(h, draft.id)
        self.assertIn("18:30", stored.publication_text, "значение не сохранено")
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            stored.publication_text,
            "анонс в чате не обновлён на месте",
        )
        self.assertEqual(
            _only_active(h, draft.id).message_ids,
            (message_id,),
            "message_id анонса изменился",
        )
        self.assertEqual(stored.delivered_message_ids, (message_id,))
        self.assertEqual(_created_since(h, start), [], "сохранение создало сообщение")
        self.assertEqual(
            _answer_texts_since(h, start),
            [],
            "после сохранения показано сообщение об успехе",
        )
        self.assertEqual(_messages_since(h, start), [], "появилось сообщение")

        # Клавиатура вернулась к списку полей с актуальным статусом.
        labels = [text for text, _data in _field_buttons(h)]
        self.assertTrue(
            any("Время начала" in label and "заполнено" in label for label in labels),
            f"список полей не показывает новое значение: {labels}",
        )
        self.assertFalse(h.live_callbacks("anq_"), "ввод поля не закрылся")
        self.assertIsNone(h.screen_message_id(), "появился служебный экран")
        _assert_one_control(h, message_id, "после сохранения пресета")

    @scenario_test
    async def test_c_manual_time_text_applies_in_place(self, h) -> None:
        """(3) ручной ввод «20:15» — тот же результат, без сообщений."""
        await h.text("/start")
        draft, _place_id = await _delivered_standard(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_id(active)
        await _open_input(h, active.id, START_ID)

        start = len(h.calls)
        await h.text("20:15")

        stored = _stored(h, draft.id)
        self.assertIn("20:15", stored.publication_text, "значение не сохранено")
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            stored.publication_text,
            "анонс не обновлён на месте",
        )
        self.assertEqual(
            _only_active(h, draft.id).message_ids,
            (message_id,),
            "message_id анонса изменился",
        )
        self.assertEqual(_created_since(h, start), [], "ввод текста создал сообщение")
        self.assertEqual(
            _answer_texts_since(h, start),
            [],
            "показано сообщение об успехе",
        )
        self.assertEqual(_messages_since(h, start), [], "появилось сообщение")
        self.assertFalse(h.live_callbacks("anq_"), "ввод поля не закрылся")
        labels = [text for text, _data in _field_buttons(h)]
        self.assertTrue(
            any("Время начала" in label and "заполнено" in label for label in labels),
            labels,
        )
        _assert_one_control(h, message_id, "после ручного ввода времени")

    @scenario_test
    async def test_d_invalid_time_keeps_value_and_keyboard(self, h) -> None:
        """Негатив: «99:99» — значение не меняется, ввод не теряется."""
        await h.text("/start")
        draft, _place_id = await _delivered_standard(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_id(active)
        await _open_input(h, active.id, START_ID)

        before = _stored(h, draft.id)
        start = len(h.calls)
        await h.text("99:99")
        after = _stored(h, draft.id)

        self.assertEqual(
            after.publication_text, before.publication_text, "значение изменилось"
        )
        self.assertEqual(after.revision, before.revision, "черновик изменён")
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            before.publication_text,
            "анонс в чате переписан",
        )
        # Ввод остаётся доступным: автор исправляет значение тем же полем.
        self.assertTrue(
            h.live_callbacks("anq_time:"),
            "клавиатура ввода потеряна после ошибки",
        )
        self.assertGreaterEqual(len(h.calls), start, "сбой не должен ломать сценарий")
        _assert_one_control(h, message_id, "после неверного ввода")

    @scenario_test
    async def test_e_repeated_preset_is_not_an_error(self, h) -> None:
        """Негатив: повторное нажатие «18:30» — не ошибка и не второй экран."""
        await h.text("/start")
        draft, _place_id = await _delivered_standard(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_id(active)
        await _open_input(h, active.id, START_ID)

        presets = _rows(_markup_for(h, message_id))[1]
        target = next(button for button in presets if button.text == "18:30")
        await h.callback(target.callback_data)

        start = len(h.calls)
        await h.callback(target.callback_data)

        stored = _stored(h, draft.id)
        self.assertIn("18:30", stored.publication_text)
        self.assertFalse(
            [
                text
                for text in _answer_texts_since(h, start)
                if "не удалось" in text.lower()
            ],
            "повтор показан ошибкой",
        )
        self.assertEqual(_created_since(h, start), [], "повтор создал сообщение")
        self.assertTrue(_field_buttons(h), "список полей не вернулся")
        _assert_one_control(h, message_id, "после повторного нажатия")


# --- 5. Текстовое поле: описание -------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TextFieldInPlaceTests(unittest.TestCase):
    """§1 п.5: подсказка и пара навигации, текст применяется сразу."""

    @scenario_test
    async def test_f_description_has_hint_only_and_applies_immediately(self, h) -> None:
        await h.text("/start")
        draft, _place_id = await _delivered_standard(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_id(active)
        await _open_input(h, active.id, DESC_ID)

        rows = _rows(_markup_for(h, message_id))
        self.assertEqual(
            _row_texts(rows),
            [[WAIT + " 📝 Описание"], [CANCEL]],
            "клавиатура текстового поля отличается от контракта",
        )
        self.assertTrue(is_disabled(rows[0][0]))
        self.assertIsNone(rows[0][0].callback_data)

        start = len(h.calls)
        await h.text("Новое описание программы")

        stored = _stored(h, draft.id)
        self.assertIn("Новое описание программы", stored.publication_text)
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            stored.publication_text,
            "анонс не обновлён на месте",
        )
        self.assertEqual(_created_since(h, start), [], "ввод описания создал сообщение")
        # Никакого «Теперь введите описание» — сообщений нет вовсе.
        self.assertEqual(_messages_since(h, start), [])
        self.assertFalse(
            [text for text in h.answer_texts() if "Теперь введите" in text],
            "появился служебный вопрос «Теперь введите…»",
        )
        self.assertTrue(_field_buttons(h), "список полей не вернулся")
        labels = [text for text, _data in _field_buttons(h)]
        self.assertTrue(
            any("Описание" in label and "заполнено" in label for label in labels),
            labels,
        )
        _assert_one_control(h, message_id, "после ввода описания")


# --- 6. «Назад», «Пропустить» и медиагруппа --------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class BackSkipAndAlbumTests(unittest.TestCase):
    """§1 п.6: навигация возвращает список, альбом живёт на одной панели."""

    @scenario_test
    async def test_g_cancel_keeps_previous_value(self, h) -> None:
        await h.text("/start")
        draft, _place_id = await _delivered_standard(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_id(active)
        await _open_input(h, active.id, START_ID)
        before = _stored(h, draft.id)

        start = len(h.calls)
        await h.callback(_input_callbacks(h, "abort")[0])
        after = _stored(h, draft.id)
        self.assertTrue(_field_buttons(h), "«Отменить» не вернул список полей")
        self.assertEqual(
            after.publication_text, before.publication_text, "значение изменилось"
        )
        self.assertEqual(after.revision, before.revision, "черновик изменён")
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            before.publication_text,
            "анонс переписан",
        )
        self.assertEqual(_created_since(h, start), [], "«Отменить» создала сообщение")
        self.assertEqual(_messages_since(h, start), [], "появилось сообщение")
        _assert_one_control(h, message_id, "после «Отменить»")

    @scenario_test
    async def test_h_album_uses_single_panel_in_place(self, h) -> None:
        """(6) 2+ фото: список и ввод — на единственной панели, без сообщений."""
        await h.text("/start")
        draft, _place_id = await _delivered_standard(h, photos=2)
        active = _only_active(h, draft.id)
        panel_id = int(active.panel_message_id)
        album_first = _announcement_id(active)
        self.assertEqual(len(_panel_sends(h)), 1, "панель должна быть одна")
        _assert_one_control(h, panel_id, "панель альбома")

        created = _created_count(h)
        calls_before = len(h.calls)
        await h.callback(f"anp_data:{active.id}")
        self.assertEqual(_created_count(h), created, "список создал сообщение")
        self.assertEqual(len(_panel_sends(h)), 1, "появилась вторая панель")
        self.assertEqual(_only_active(h, draft.id).panel_message_id, panel_id)
        # Подпись альбома не переписывается ни списком полей, ни вводом.
        self.assertEqual(
            [
                call["type"]
                for call in h.calls[calls_before:]
                if call["type"] in {"edit_caption", "edit_media"}
            ],
            [],
            "список полей переписал подпись альбома",
        )
        _assert_one_control(h, panel_id, "список полей в панели")

        start = len(h.calls)
        await h.callback(_field_callback(h, START_ID))
        self.assertEqual(_created_since(h, start), [], "выбор поля создал сообщение")
        self.assertEqual(len(_panel_sends(h)), 1, "появилась вторая панель")
        rows = _rows(_markup_for(h, panel_id))
        self.assertEqual(
            _row_texts(rows),
            [[WAIT + " 🕒 Время начала"], list(PRESETS), [CANCEL]],
            "клавиатура ввода не в панели",
        )
        self.assertTrue(is_disabled(rows[0][0]))
        _assert_one_control(h, panel_id, "ввод в панели")

        target = next(button for button in rows[1] if button.text == "18:30")
        start = len(h.calls)
        await h.callback(target.callback_data)

        stored = _stored(h, draft.id)
        self.assertIn("18:30", stored.publication_text)
        self.assertEqual(_created_since(h, start), [], "сохранение создало сообщение")
        self.assertEqual(len(_panel_sends(h)), 1, "появилась вторая панель")
        self.assertEqual(
            _only_active(h, draft.id).panel_message_id,
            panel_id,
            "адрес панели изменился",
        )
        self.assertEqual(
            h.bot.content_text(h.chat_id, album_first),
            stored.publication_text,
            "подпись альбома не обновлена",
        )
        # Список полей вернулся В ПАНЕЛЬ, второй панели нет.
        self.assertEqual(
            h.bot._message_content[(h.chat_id, panel_id)][1],
            _field_list_body(),
            "панель не вернулась к списку полей",
        )
        self.assertTrue(_field_buttons(h), "списка полей в панели нет")
        _assert_one_control(h, panel_id, "после сохранения в альбоме")


# --- 8. Дата и контакт -----------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DateAndContactTests(unittest.TestCase):
    """§1 п.8: дата — календарь на клавиатуре анонса, контакт — та же схема."""

    @scenario_test
    async def test_i_date_calendar_on_announcement_keyboard(self, h) -> None:
        await h.text("/start")
        draft, _place_id = await _delivered_standard(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_id(active)
        await _open_input(h, active.id, DATE_ID)

        markup = _markup_for(h, message_id)
        rows = _rows(markup)
        # У поля-даты подсказки нет: календарь сам объясняет, чего ждут
        # (2.5.5 §1). Последняя строка — одна «Отменить».
        self.assertFalse(
            [button for row in rows for button in row if is_disabled(button)],
            "у даты не должно быть неактивной подсказки",
        )
        self.assertEqual(rows[-1][0].text, CANCEL)
        self.assertTrue(rows[-1][0].callback_data.startswith("anq_abort:"))
        callbacks = [
            button.callback_data
            for row in rows
            for button in row
            if button.callback_data
        ]
        self.assertTrue(
            any(data.startswith("anq_day:") for data in callbacks),
            f"нет дней календаря: {callbacks}",
        )
        self.assertTrue(
            any(data.startswith("anq_cal:") for data in callbacks),
            f"нет перехода к месяцам: {callbacks}",
        )
        self.assertIsNone(h.screen_message_id(), "календарь — отдельный экран")
        _assert_one_control(h, message_id, "календарь на анонсе")

        start = len(h.calls)
        day = next(data for data in callbacks if data.startswith("anq_day:"))
        await h.callback(day)

        stored = _stored(h, draft.id)
        # Дата в анонсе — короткий формат шаблона (день и месяц).
        self.assertRegex(
            stored.publication_text,
            r"\d{2}\.\d{2}",
        )
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            stored.publication_text,
            "дата не применена к анонсу",
        )
        self.assertEqual(_created_since(h, start), [], "выбор даты создал сообщение")
        self.assertTrue(_field_buttons(h), "список полей не вернулся")
        labels = [text for text, _data in _field_buttons(h)]
        self.assertTrue(
            any("Дата" in label and "заполнено" in label for label in labels),
            labels,
        )
        _assert_one_control(h, message_id, "после выбора даты")

    @scenario_test
    async def test_j_contact_uses_same_scheme(self, h) -> None:
        await h.text("/start")
        draft, _place_id = await _delivered_standard(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_id(active)
        await _open_input(h, active.id, CONTACT_ID)

        rows = _rows(_markup_for(h, message_id))
        self.assertEqual(
            _row_texts(rows),
            [[WAIT + " ☎️ Контакт"], [CANCEL]],
            "клавиатура контакта отличается от контракта",
        )
        self.assertIsNone(rows[0][0].callback_data)
        self.assertTrue(is_disabled(rows[0][0]))

        start = len(h.calls)
        await h.text("+7 900 111-22-33")
        stored = _stored(h, draft.id)
        self.assertIn("+7 900 111-22-33", stored.publication_text)
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            stored.publication_text,
            "контакт не применён к анонсу",
        )
        self.assertEqual(_created_since(h, start), [], "ввод контакта создал сообщение")
        self.assertEqual(_messages_since(h, start), [])
        self.assertTrue(_field_buttons(h), "список полей не вернулся")
        _assert_one_control(h, message_id, "после ввода контакта")


# --- 7. Одно место управления после каждого шага ---------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class SingleControlKeyboardTests(unittest.TestCase):
    """§1 п.7: после каждого шага — ровно одна управляющая клавиатура."""

    @scenario_test
    async def test_k_exactly_one_control_keyboard_after_each_step(self, h) -> None:
        await h.text("/start")
        draft, _place_id = await _delivered_standard(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_id(active)
        _assert_one_control(h, message_id, "после доставки")

        await h.callback(f"anp_data:{active.id}")
        _assert_one_control(h, message_id, "список полей")

        await h.callback(_field_callback(h, START_ID))
        _assert_one_control(h, message_id, "ввод времени")

        preset = next(
            button
            for button in _rows(_markup_for(h, message_id))[1]
            if button.text == "18:30"
        )
        await h.callback(preset.callback_data)
        _assert_one_control(h, message_id, "сохранение времени")

        await h.callback(_field_callback(h, NAME_ID))
        _assert_one_control(h, message_id, "ввод названия")
        await h.text("Новое название")
        _assert_one_control(h, message_id, "сохранение названия")

        # «Назад» из списка возвращает управление анонсу — место то же.
        await h.callback(h.live_callbacks("anf_back:")[0])
        _assert_one_control(h, message_id, "возврат к управлению анонсом")
        self.assertTrue(
            h.live_callbacks(f"anp_data:{active.id}"),
            "управление анонсом не вернулось",
        )
        # Ни на одном шаге не появилось второго места управления.
        self.assertEqual(
            list(_control_keyboards(h)), [message_id], "клавиатур больше одной"
        )


# --- 4. Рестарт ------------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class RestartTests(unittest.TestCase):
    """§1 п.7/4: управление продолжает работать после рестарта."""

    @scenario_test
    async def test_l_field_management_survives_restart(self, h) -> None:
        await h.text("/start")
        draft, _place_id = await _delivered_standard(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_id(active)
        await _open_input(h, active.id, START_ID)

        fresh = await _restart_with_new_harness(h)
        try:
            # Состояний диалога нет — как после рестарта службы.
            self.assertIsNone(fresh.dialog_state(), "состояние диалога сохранилось")
            self.assertEqual(fresh.user_data(), {}, "user_data сохранились")
            # Клавиатура ввода осталась в чате: это сообщение Telegram.
            self.assertTrue(
                fresh.live_callbacks("anq_time:"),
                "клавиатура ввода пропала после рестарта",
            )

            created = _created_count(fresh)
            # «Назад» восстанавливает список без состояния диалога.
            await fresh.callback(fresh.live_callbacks("anq_abort:")[0])
            self.assertTrue(_field_buttons(fresh), "список полей не открылся")
            self.assertEqual(_created_count(fresh), created, "список создал сообщение")
            _assert_one_control(fresh, message_id, "список после рестарта")

            await fresh.callback(_field_callback(fresh, START_ID))
            presets = [
                data
                for _label, data in fresh.live_buttons()
                if data and data.startswith("anq_time:")
            ]
            target = next(data for data in presets if data.endswith(":18:30"))

            start = len(fresh.calls)
            created = _created_count(fresh)
            await fresh.callback(target)

            stored = _stored(fresh, draft.id)
            self.assertIn("18:30", stored.publication_text, "значение не сохранено")
            self.assertEqual(
                _created_count(fresh), created, "после рестарта создано сообщение"
            )
            self.assertEqual(
                fresh.bot.content_text(fresh.chat_id, message_id),
                stored.publication_text,
                "анонс не обновлён на месте",
            )
            self.assertEqual(
                _only_active(fresh, draft.id).message_ids,
                (message_id,),
                "message_id анонса изменился",
            )
            self.assertTrue(_field_buttons(fresh), "список полей не вернулся")
            self.assertEqual(_created_since(fresh, start), [])
            _assert_one_control(fresh, message_id, "сохранение после рестарта")
        finally:
            await fresh.close()


# --- 9. Поля площадки ------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlaceFieldsProtectionTests(unittest.TestCase):
    """§1: поля площадки не редактируются в анонсе и не подделываются."""

    @scenario_test
    async def test_m_place_fields_absent_and_forged_callbacks_change_nothing(
        self, h
    ) -> None:
        await h.text("/start")
        draft, place_id = await _delivered_standard(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_id(active)

        # Поля площадки действительно есть в анонсе (значения из справочника).
        with h.session_factory() as session:
            fields = build_announcement_fields(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
        known = {field.code for field in fields if field.status == FIELD_STATUS_KNOWN}
        self.assertLessEqual(
            set(PLACE_CODES), known, "поля площадки отсутствуют в анонсе"
        )

        await h.callback(f"anp_data:{active.id}")
        labels = " | ".join(label for label, _data in _field_buttons(h))
        for title in PLACE_TITLES:
            self.assertNotIn(title, labels, f"поле площадки {title!r} в редакторе")
        for title in EDITABLE_TITLES:
            self.assertIn(title, labels, f"текстового поля {title!r} нет в редакторе")

        before_text = _stored(h, draft.id).publication_text
        draft_row = _draft_row(h, draft.id)
        value_rows = _value_rows(h, draft.id)
        place_row = _place_row(h, place_id)
        created = _created_count(h)

        # Поддельный выбор поля площадки: ввод не открывается, данных нет.
        for brick_id in PLACE_IDS:
            start = len(h.calls)
            await h.callback(f"anf_sel:{active.id}:{brick_id}")
            self.assertFalse(
                h.live_callbacks("anq_"),
                f"поддельный выбор поля #{brick_id} открыл ввод",
            )
            self.assertEqual(
                _created_since(h, start),
                [],
                f"поддельный выбор поля #{brick_id} создал сообщение",
            )

        # Поддельная кнопка ввода на поле площадки: значение не пишется.
        for brick_id in PLACE_IDS:
            start = len(h.calls)
            await h.callback(f"anq_time:{active.id}:{brick_id}:18:00")
            self.assertEqual(
                _created_since(h, start),
                [],
                f"поддельный ввод поля #{brick_id} создал сообщение",
            )

        self.assertEqual(
            _stored(h, draft.id).publication_text,
            before_text,
            "данные анонса изменились",
        )
        self.assertEqual(_draft_row(h, draft.id), draft_row, "черновик изменён")
        self.assertEqual(_value_rows(h, draft.id), value_rows, "значения изменены")
        self.assertEqual(_place_row(h, place_id), place_row, "площадка изменена")
        self.assertEqual(_created_count(h), created, "появились сообщения")
        _assert_one_control(h, message_id, "после поддельных callback")


# --- Сбой штатной правки ---------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class InPlaceFailureTests(unittest.TestCase):
    """Сбой Telegram: значение не теряется, место управления остаётся одно."""

    @scenario_test
    async def test_n_caption_edit_failure_keeps_value_and_single_control(
        self, h
    ) -> None:
        await h.text("/start")
        draft, _place_id = await _delivered_standard(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _announcement_id(active)
        await _open_input(h, active.id, START_ID)

        target = next(
            button
            for button in _rows(_markup_for(h, message_id))[1]
            if button.text == "18:30"
        )
        # Telegram не даёт править подпись (сообщение удалено/недоступно).
        h.bot.errors_by_type["edit_caption"] = BadRequest("Message to edit not found")
        try:
            await h.callback(target.callback_data)
        finally:
            h.bot.errors_by_type.pop("edit_caption", None)

        stored = _stored(h, draft.id)
        self.assertIn("18:30", stored.publication_text, "значение потеряно")

        # Восстановление не оставляет двух мест управления.
        found = _control_keyboards(h)
        self.assertEqual(len(found), 1, f"управляющих клавиатур {len(found)}: {found}")
        new_active = _only_active(h, draft.id)
        new_id = _announcement_id(new_active)
        self.assertEqual(
            h.bot.content_text(h.chat_id, new_id),
            stored.publication_text,
            "восстановленный анонс не показывает сохранённое значение",
        )
        # Прежняя версия остаётся снимком без кнопок.
        self.assertNotIn(message_id, h.deleted_ids(), "прежняя версия удалена")
        self.assertIsNone(
            _markup_signature(h, message_id),
            "у прежней версии остались кнопки",
        )
        self.assertEqual(
            sorted(found), [new_id], "управление не на восстановленном анонсе"
        )


if __name__ == "__main__":
    unittest.main()
