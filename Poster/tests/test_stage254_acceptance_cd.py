# tests/test_stage254_acceptance_cd.py
"""
Poster 2.5.4 §3 + приёмка C/D/E §9 — независимая проверка ПОЛЬЗОВАТЕЛЬСКОГО
контракта «Превышение длины текста» и «Отмена незавершённой правки».

Проверяется требование, а не реализация: сценарии идут полным путём автора
через настоящее приложение (`bot.build_application`), FakeBot и временную
SQLite (`tests/integration_harness.py`). Реальная `post_bot.db`, `.env` и сеть
Telegram не используются.

Что закрепляется (по пунктам задания):

C. Анонс без фото, готовый. Правка поля «Описание» текстом, после которого
   весь итоговый анонс больше 4096 символов:
   1) текст анонса и его сообщение не изменились;
   2) введённый текст сохранён целиком как незавершённая правка;
   3) сообщение пользователя с длинным текстом осталось в чате;
   4) рекомендаций про удаление фотографий нет (их нет);
   5) на управлении ровно две кнопки: неактивная подсказка
      «⏳ Ожидаю текст короче» (disabled, без callback_data) и «Отменить»
      (стиль danger в сериализации); никаких «Назад», «Пропустить»,
      «Обновить», «Обзор анонса», «Описание — 800», «Изменить другое поле»,
      «Без фотографий»;
   6) ошибка короткая, называет лимит 4096 и говорит, что данные сохранены.
   C-исправление: подходящий текст применяется сразу, возвращается список
   полей, кнопок «Применить/Сохранить» нет.
   C-рестарт: незавершённая правка переживает рестарт службы, но анонс в
   Telegram остаётся прежним; короткий текст после рестарта применяется.
D. Анонс с одним фото. Подпись больше 1024: опубликованное сообщение
   (текст/фото/message_id) не изменилось, правка сохранена и переживает
   рестарт, сообщение пользователя не удалено, клавиатура ошибки та же,
   лимит 1024 назван; исправление даёт РОВНО один `editMessageCaption` и не
   создаёт новой управляющей панели.
E. «Отменить» возвращает только это поле к опубликованному значению, не
   трогает другие поля, не публикует заново, возвращает актуальный список
   полей; повторное и позднее нажатие старого callback ничего не откатывает.
Гонка: после успешного сохранения поздний callback старого экрана ошибки не
   отменяет новое значение.
Границы: ровно 4096 (и 1024 для подписи) проходят, на единицу больше — нет.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage254_acceptance_cd -v
"""

from __future__ import annotations

import asyncio
import json
import re
import unittest

from tests import ui_texts_expect as cat
from utils.ui_texts import tr

try:
    from sqlalchemy import text

    import announcement_controls as controls
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
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
    from tests.test_stage253_acceptance_final import _value_rows
    from tests.test_stage253_text_fields_e2 import _restart_with_new_harness
    from utils.inline_buttons import is_disabled

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


# Требования владельца: тексты и пределы, а не значения из каталога.
PHOTOS_TITLE = tr("wizard.photos.title")
HINT = "⏳ Ожидаю текст короче"
CANCEL = "Отменить"
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024

OLD_DESCRIPTION = "Старое описание"
OLD_SUBTITLE = "Подзаголовок"
NEW_SUBTITLE = "Новый подзаголовок"
FIXED_DESCRIPTION = "Исправленное описание"
MIDDLE_DESCRIPTION = "Среднее описание"
SHORT_CAPTION = "Короткая подпись"

# Значения с запасом: шаблонная обвязка («Строка 0: ») не решает исход.
LONG_TEXT = "а" * 4200
LONG_CAPTION = "б" * 1100

# Кнопки, которых на экране ошибки длины быть не должно (§3).
FORBIDDEN_LABELS = (
    "Назад",
    "Пропустить",
    "Обновить",
    "Обзор",
    "Применить",
    "Сохранить",
    "Изменить другое",
    "Без фотографий",
    "фото",
)

# Типы вызовов, создающих НОВОЕ сообщение в чате.
CREATE_TYPES = frozenset({"message", "photo", "media_group", "rich_message"})

SPECS: list[tuple[str, str, str, dict[str, object]]] = [
    ("f0", "Описание", "long_text", {}),
    ("f1", "Подзаголовок", "short_text", {}),
]
CONTENT = "Строка 0: [f0]\nСтрока 1: [f1]"


# --- помощники -------------------------------------------------------------


def _created_count(h) -> int:
    """Сколько вызовов создали сообщения бота (без правок и удалений)."""
    return sum(1 for call in h.calls if call["type"] in CREATE_TYPES)


def _created_since(h, start: int) -> list[dict]:
    return [call for call in h.calls[start:] if call["type"] in CREATE_TYPES]


def _markup_for(h, message_id: int):
    """
    Клавиатура КОНКРЕТНОГО сообщения на текущий момент.

    У анонса 0–1 фото это `editMessageReplyMarkup`, у панели медиагруппы —
    `editMessageText`; берётся последний вызов с `reply_markup` для этого
    `message_id`.
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


def _rows(h, message_id: int) -> list[list]:
    markup = _markup_for(h, message_id)
    if markup is None or not hasattr(markup, "inline_keyboard"):
        return []
    return [list(row) for row in (markup.inline_keyboard or [])]


def _row_texts(rows: list[list]) -> list[list[str]]:
    return [[button.text for button in row] for row in rows]


def _field_buttons(h) -> list[tuple[str, str]]:
    """Живые кнопки списка полей (подпись, `anf_sel:` callback)."""
    return [
        (label, data)
        for label, data in h.live_buttons()
        if data and data.startswith("anf_sel:")
    ]


def _control_keyboards(h) -> dict[int, list[str]]:
    """Живые сообщения с кнопками управления/правки анонса (`an[pfq]_`)."""
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


def _limit_notices(h, limit: int) -> list[str]:
    """Сообщения автора про превышение с названным лимитом."""
    out: list[str] = []
    for text in h.reply_texts() + h.answer_texts():
        low = text.lower()
        if str(limit) in text and "текст" in low:
            out.append(text)
    return out


async def _delivered(h, *, photos: int = 0):
    """Личный шаблон из двух полей → ответы → фото → доставка."""
    place_id, _alias = _seed_place(h)
    template_id, _revision, bricks = _seed_personal_template(
        h, name="Шаблон §3", specs=SPECS, content=CONTENT
    )
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    answers = [OLD_DESCRIPTION, OLD_SUBTITLE]
    for index in range(len(SPECS) + 6):
        if PHOTOS_TITLE in h.screen_text():
            break
        await h.text(answers[index] if index < len(answers) else "Значение")
    if PHOTOS_TITLE not in h.screen_text():
        raise AssertionError(f"не шаг фото: {h.screen_text()[:200]!r}")
    if photos == 0:
        data = next(
            data
            for _label, data in h.screen_buttons()
            if data and data.startswith("aw_photo_none")
        )
        await h.callback(data)
    elif photos == 1:
        await h.photo("photo-0")
    else:
        for index in range(photos):
            await h.photo(f"photo-{index}", media_group_id="album")
        await asyncio.sleep(0.4)
    draft = _latest_draft(h)
    if _stored(h, draft.id).publication_status != "sent":
        raise AssertionError(f"анонс не доставлен: {h.screen_text()[:200]!r}")
    return draft, bricks


async def _open_description(h, instance_id: int) -> None:
    """Открывает ввод поля «Описание» на управлении анонса."""
    if not _field_buttons(h):
        await h.callback(controls.panel_callback(controls.ACTION_DATA, instance_id))
    data = next(data for label, data in _field_buttons(h) if "Описание" in label)
    await h.callback(data)


async def _open_subtitle(h, instance_id: int) -> None:
    if not _field_buttons(h):
        await h.callback(controls.panel_callback(controls.ACTION_DATA, instance_id))
    data = next(data for label, data in _field_buttons(h) if "Подзаголовок" in label)
    await h.callback(data)


def _message_id(h, draft_id: int) -> int:
    return int(_only_active(h, draft_id).message_ids[0])


def _field_value(h, draft_id: int, brick_id: int) -> str:
    """Текст значения поля из БД (полнота сохранённого ввода)."""
    row = next(row for row in _value_rows(h, draft_id) if row[0] == brick_id)
    payload = json.loads(row[2] or "{}")
    text_value = payload.get("text")
    return text_value if isinstance(text_value, str) else ""


def _stored_photo_marker(h, message_id: int) -> str | None:
    """Подпись фото-сообщения (у фотографии — её caption)."""
    return h.bot.content_text(h.chat_id, message_id)


def _assert_error_keyboard(test: unittest.TestCase, h, message_id: int) -> None:
    """Ровно две кнопки: подсказка (disabled, без callback) и «Отменить»."""
    rows = _rows(h, message_id)
    test.assertEqual(
        _row_texts(rows), [[HINT], [CANCEL]], "клавиатура ошибки не по контракту"
    )
    hint = rows[0][0]
    test.assertTrue(is_disabled(hint), "подсказка нажимается")
    test.assertIsNone(hint.callback_data, "у подсказки есть callback_data")
    cancel = rows[1][0]
    test.assertTrue(
        (cancel.callback_data or "").startswith("anq_cancel:"),
        f"«Отменить» без callback отмены: {cancel.callback_data}",
    )

    payload = _markup_for(h, message_id).to_dict()["inline_keyboard"]
    test.assertIn("disabled", payload[0][0], "disabled не сериализуется")
    test.assertNotIn("callback_data", payload[0][0], "подсказка кликабельна")
    test.assertEqual(
        payload[1][0].get("style"), "danger", "«Отменить» без стиля danger"
    )

    labels = " | ".join(button.text for row in rows for button in row)
    for marker in FORBIDDEN_LABELS:
        test.assertNotIn(marker, labels, f"на экране ошибки есть {marker!r}: {labels}")


# --- C. без фото: >4096, анонс цел, ровно две кнопки -----------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class NoPhotoLengthErrorTests(unittest.TestCase):
    """§3/приёмка C: непоместившийся текст не публикуется и не теряется."""

    @scenario_test
    async def test_c1_error_screen_keeps_announcement_and_full_input(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=0)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)
        published = _stored(h, draft.id).publication_text
        self.assertEqual(
            published, f"Строка 0: {OLD_DESCRIPTION}\nСтрока 1: {OLD_SUBTITLE}"
        )
        posts_before = list(h.posts())
        await _open_description(h, active.id)

        user_message = await h.text(LONG_TEXT)

        # (4)(6) Короткая причина: лимит 4096 назван, данные сохранены,
        # про фотографии речи нет.
        notices = _limit_notices(h, TEXT_LIMIT)
        self.assertTrue(notices, f"нет ошибки с лимитом: {h.reply_texts()}")
        notice = notices[-1]
        # Отказ по длине — строка каталога правки доставленного анонса:
        # она называет лимит и сообщает, что анонс не изменился. Полнота
        # сохранённого ввода проверяется ниже (`_field_value`).
        cat.assert_shows(
            self,
            notice,
            "wizard.length_refusal.edit_not_applied",
            limit=TEXT_LIMIT,
        )
        low = notice.lower()
        self.assertIn(
            "не изменился", low, f"не сказано, что анонс не изменён: {notice!r}"
        )
        self.assertNotIn("фот", low, f"без фото упомянуты фото: {notice!r}")
        self.assertLess(len(notice), 400, "причина слишком длинная")

        # (5) Ровно две кнопки, подсказка неактивна, «Отменить» — danger.
        _assert_error_keyboard(self, h, message_id)

        # (1) Анонс и его сообщение не изменились.
        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_text, published, "анонс переписан")
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.delivered_message_ids, (message_id,))
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), published, "пост изменён"
        )
        self.assertEqual(h.posts(), posts_before, "лишняя публикация")

        # (3) Ввод автора не удалён.
        self.assertNotIn(
            user_message.message.message_id,
            h.deleted_ids(),
            "сообщение с длинным текстом удалено",
        )

        # (2) Текст сохранён целиком как незавершённая правка.
        self.assertEqual(
            _field_value(h, draft.id, bricks["f0"]), LONG_TEXT, "ввод потерян"
        )
        # Единственное живое управление — тот же анонс.
        self.assertEqual(
            list(_control_keyboards(h)), [message_id], "второе место управления"
        )

    @scenario_test
    async def test_c2_correction_applies_in_place_and_returns_list(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=0)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)
        await _open_description(h, active.id)
        await h.text(LONG_TEXT)

        start = len(h.calls)
        await h.text(FIXED_DESCRIPTION)

        stored = _stored(h, draft.id)
        self.assertIn(FIXED_DESCRIPTION, stored.publication_text, "правка не применена")
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
        # Список полей вернулся, кнопок «Применить/Сохранить» нет.
        labels = " | ".join(label for label, _data in _field_buttons(h))
        self.assertIn("Описание", labels, "список полей не вернулся")
        for marker in ("Применить", "Сохранить"):
            self.assertNotIn(marker, labels, f"появилась кнопка {marker!r}")
        self.assertFalse(h.live_callbacks("anq_"), "ввод поля не закрылся")
        self.assertEqual(_created_since(h, start), [], "правка создала сообщение")
        self.assertEqual(
            list(_control_keyboards(h)), [message_id], "второе место управления"
        )

    @scenario_test
    async def test_c3_pending_edit_survives_restart_but_not_applied(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=0)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)
        published = _stored(h, draft.id).publication_text
        posts_before = list(h.posts())
        await _open_description(h, active.id)
        await h.text(LONG_TEXT)

        fresh = await _restart_with_new_harness(h)
        try:
            # Рестарт: состояний диалога нет, публикация прежняя.
            self.assertIsNone(fresh.dialog_state(), "состояние диалога сохранилось")
            self.assertEqual(fresh.user_data(), {}, "user_data сохранились")
            self.assertEqual(
                _stored(fresh, draft.id).publication_text,
                published,
                "после рестарта анонс считается применённым",
            )
            self.assertEqual(fresh.posts(), posts_before, "лишняя публикация")
            self.assertEqual(
                fresh.bot.content_text(fresh.chat_id, message_id),
                published,
                "сообщение анонса изменилось",
            )
            # Ввод целиком на месте.
            self.assertEqual(
                _field_value(fresh, draft.id, bricks["f0"]),
                LONG_TEXT,
                "незавершённая правка потеряна",
            )

            # Короткий текст после рестарта применяется на месте.
            created = _created_count(fresh)
            await fresh.text(FIXED_DESCRIPTION)
            stored = _stored(fresh, draft.id)
            self.assertIn(FIXED_DESCRIPTION, stored.publication_text)
            self.assertEqual(
                fresh.bot.content_text(fresh.chat_id, message_id),
                stored.publication_text,
                "анонс не обновлён на месте после рестарта",
            )
            self.assertEqual(_created_count(fresh), created, "создано сообщение")
            self.assertTrue(_field_buttons(fresh), "список полей не вернулся")
        finally:
            await fresh.close()

    @scenario_test
    async def test_c4_exact_limit_passes_one_character_over_fails(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=0)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)
        await _open_description(h, active.id)
        await h.text(LONG_TEXT)

        notice = _limit_notices(h, TEXT_LIMIT)[-1]
        match = re.search(r"(\d+)\s+символ", notice)
        self.assertIsNotNone(match, f"в ошибке нет числа символов: {notice!r}")
        assert match is not None
        total = int(match.group(1))
        excess = total - TEXT_LIMIT
        self.assertGreater(excess, 0, "ошибка показана без превышения лимита")

        # Ровно предел — применяется.
        boundary = LONG_TEXT[: len(LONG_TEXT) - excess]
        await h.text(boundary)
        stored = _stored(h, draft.id)
        self.assertEqual(
            len(stored.publication_text),
            TEXT_LIMIT,
            "ровно 4096 символов не применились",
        )
        self.assertIn(boundary, stored.publication_text)
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            stored.publication_text,
            "анонс не обновлён",
        )

        # На единицу больше — снова экран ошибки, анонс не меняется.
        await _open_description(h, active.id)
        before = _stored(h, draft.id).publication_text
        start = len(h.calls)
        await h.text(boundary + "Я")
        self.assertEqual(_stored(h, draft.id).publication_text, before)
        self.assertTrue(_limit_notices(h, TEXT_LIMIT), "нет ошибки на 4097 символов")
        _assert_error_keyboard(self, h, message_id)
        self.assertEqual(
            [c["type"] for c in _created_since(h, start)],
            ["message"],
            "на ошибке появилось лишнее сообщение",
        )

    @scenario_test
    async def test_c5_telegram_edit_failure_keeps_data_and_explains(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=0)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)
        published = _stored(h, draft.id).publication_text
        await _open_description(h, active.id)

        # Telegram не даёт править клавиатуру анонса.
        h.bot.fail_types.add("edit_markup")
        try:
            await h.text(LONG_TEXT)
        finally:
            h.bot.fail_types.discard("edit_markup")

        # Сбой не теряет данные и не публикует длинный текст.
        self.assertEqual(
            _field_value(h, draft.id, bricks["f0"]), LONG_TEXT, "ввод потерян"
        )
        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_text, published, "анонс переписан")
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), published, "пост изменён"
        )
        self.assertTrue(_limit_notices(h, TEXT_LIMIT), "автор не увидел причину")
        # Место управления одно: клавиатура анонса осталась прежней.
        self.assertEqual(len(_control_keyboards(h)), 1, _control_keyboards(h))


# --- D. одно фото: подпись >1024, одно обновление --------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CaptionLengthTests(unittest.TestCase):
    """§3/приёмка D: лимит подписи 1024, правка не публикуется."""

    @scenario_test
    async def test_d1_caption_error_keeps_published_post(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)
        published = _stored(h, draft.id).publication_text
        caption_before = _stored_photo_marker(h, message_id)
        self.assertEqual(caption_before, published)
        photos_before = [c for c in h.calls if c["type"] == "photo"]
        posts_before = list(h.posts())
        await _open_description(h, active.id)

        start = len(h.calls)
        user_message = await h.text(LONG_CAPTION)

        notices = _limit_notices(h, CAPTION_LIMIT)
        self.assertTrue(notices, f"нет ошибки с лимитом 1024: {h.reply_texts()}")
        notice = notices[-1]
        cat.assert_shows(
            self,
            notice,
            "wizard.length_refusal.edit_not_applied",
            limit=CAPTION_LIMIT,
        )
        self.assertLess(len(notice), 400, "причина слишком длинная")

        _assert_error_keyboard(self, h, message_id)

        # Опубликованное не изменилось: ни подпись, ни фото, ни адрес.
        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_text, published, "анонс переписан")
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.delivered_message_ids, (message_id,))
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            caption_before,
            "подпись фото изменена",
        )
        self.assertEqual(
            [c for c in h.calls if c["type"] == "photo"],
            photos_before,
            "фото переопубликовано",
        )
        self.assertEqual(h.posts(), posts_before, "лишняя публикация")
        self.assertEqual(
            [c["type"] for c in h.calls[start:] if c["type"] == "edit_caption"],
            [],
            "подпись переписана",
        )
        self.assertNotIn(
            user_message.message.message_id,
            h.deleted_ids(),
            "сообщение с длинной подписью удалено",
        )
        self.assertEqual(
            _field_value(h, draft.id, bricks["f0"]),
            LONG_CAPTION,
            "ввод потерян",
        )
        self.assertEqual(len(_panel_sends(h)), 0, "появилась управляющая панель")
        self.assertEqual(
            list(_control_keyboards(h)), [message_id], "второе место управления"
        )

    @scenario_test
    async def test_d2_fix_updates_caption_once_and_returns_list(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)
        await _open_description(h, active.id)
        await h.text(LONG_CAPTION)

        start = len(h.calls)
        await h.text(SHORT_CAPTION)

        stored = _stored(h, draft.id)
        self.assertIn(SHORT_CAPTION, stored.publication_text)
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            stored.publication_text,
            "подпись не обновлена",
        )
        caption_edits = [c for c in h.calls[start:] if c["type"] == "edit_caption"]
        self.assertEqual(
            len(caption_edits), 1, f"обновлений подписи {len(caption_edits)}"
        )
        self.assertEqual(int(caption_edits[0]["message_id"]), message_id)
        # Ни новой панели, ни новых сообщений интерфейса.
        self.assertEqual(len(_panel_sends(h)), 0, "появилась вторая панель")
        self.assertEqual(_created_since(h, start), [], "создано сообщение интерфейса")
        labels = " | ".join(label for label, _data in _field_buttons(h))
        self.assertIn("Описание", labels, "список полей не вернулся")
        self.assertFalse(h.live_callbacks("anq_"), "ввод поля не закрылся")
        self.assertEqual(
            list(_control_keyboards(h)), [message_id], "второе место управления"
        )

    @scenario_test
    async def test_d3_pending_caption_survives_restart(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)
        published = _stored(h, draft.id).publication_text
        await _open_description(h, active.id)
        await h.text(LONG_CAPTION)

        fresh = await _restart_with_new_harness(h)
        try:
            self.assertEqual(
                _stored(fresh, draft.id).publication_text,
                published,
                "после рестарта правка считается применённой",
            )
            self.assertEqual(
                fresh.bot.content_text(fresh.chat_id, message_id),
                published,
                "подпись изменилась после рестарта",
            )
            self.assertEqual(
                _field_value(fresh, draft.id, bricks["f0"]),
                LONG_CAPTION,
                "незавершённая правка потеряна",
            )
            start = len(fresh.calls)
            await fresh.text(SHORT_CAPTION)
            stored = _stored(fresh, draft.id)
            self.assertIn(SHORT_CAPTION, stored.publication_text)
            self.assertEqual(
                len([c for c in fresh.calls[start:] if c["type"] == "edit_caption"]),
                1,
                "подпись обновлена не один раз",
            )
            self.assertEqual(
                fresh.bot.content_text(fresh.chat_id, message_id),
                stored.publication_text,
            )
            self.assertEqual(len(_panel_sends(fresh)), 0, "появилась панель")
        finally:
            await fresh.close()


# --- 2+ фото: экран ошибки — в единственной панели -------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class MediaGroupLengthErrorTests(unittest.TestCase):
    """§3: у медиагруппы причина и две кнопки живут в её единственной панели."""

    @scenario_test
    async def test_media_group_error_in_single_panel(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=2)
        active = _only_active(h, draft.id)
        panel_id = int(active.panel_message_id)
        published = _stored(h, draft.id).publication_text
        self.assertEqual(len(_panel_sends(h)), 1, "панель должна быть одна")
        posts_before = list(h.posts())
        await _open_description(h, active.id)

        start = len(h.calls)
        await h.text(LONG_TEXT)

        # Причина названа в панели (подпись альбома не трогается).
        panel_text = h.bot.content_text(h.chat_id, panel_id) or ""
        cat.assert_shows(
            self,
            panel_text,
            "wizard.length_refusal.edit_not_applied",
            msg="лимит не назван в панели",
            limit=CAPTION_LIMIT,
        )
        # Ровно две кнопки у панели.
        _assert_error_keyboard(self, h, panel_id)
        # Вторая панель не появилась, новых сообщений интерфейса нет.
        self.assertEqual(len(_panel_sends(h)), 1, "появилась вторая панель")
        self.assertEqual(
            _only_active(h, draft.id).panel_message_id, panel_id, "адрес панели сменён"
        )
        self.assertEqual(
            [c["type"] for c in _created_since(h, start)],
            [],
            "экран ошибки создал сообщение",
        )
        # Публикация не тронута.
        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_text, published)
        self.assertEqual(h.posts(), posts_before)
        self.assertEqual(
            _field_value(h, draft.id, bricks["f0"]), LONG_TEXT, "ввод потерян"
        )
        self.assertEqual(
            list(_control_keyboards(h)), [panel_id], "второе место управления"
        )

        # Исправление применяется к подписи первого фото, панель — список полей.
        start = len(h.calls)
        await h.text(SHORT_CAPTION)
        stored = _stored(h, draft.id)
        self.assertIn(SHORT_CAPTION, stored.publication_text)
        self.assertEqual(
            len([c for c in h.calls[start:] if c["type"] == "edit_caption"]),
            1,
            "подпись обновлена не один раз",
        )
        self.assertEqual(len(_panel_sends(h)), 1, "появилась вторая панель")
        self.assertTrue(_field_buttons(h), "список полей не вернулся")
        self.assertEqual(
            list(_control_keyboards(h)), [panel_id], "второе место управления"
        )


# --- E. «Отменить» ---------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CancelLengthEditTests(unittest.TestCase):
    """§3/приёмка E: отмена возвращает только это поле и ничего не публикует."""

    @scenario_test
    async def test_e1_cancel_restores_only_edited_field(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=0)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)

        # Сначала успешно меняем ДРУГОЕ поле — его отмена трогать не должна.
        await _open_subtitle(h, active.id)
        await h.text(NEW_SUBTITLE)
        after_subtitle = _stored(h, draft.id).publication_text
        self.assertIn(NEW_SUBTITLE, after_subtitle)
        posts_before = list(h.posts())

        await _open_description(h, active.id)
        await h.text(LONG_TEXT)
        cancel = h.live_callbacks("anq_cancel:")[0]
        self.assertEqual(
            _field_value(h, draft.id, bricks["f0"]), LONG_TEXT, "ввод не сохранён"
        )

        start = len(h.calls)
        await h.callback(cancel)

        # (1) В редакторе снова опубликованное значение поля.
        self.assertEqual(
            _field_value(h, draft.id, bricks["f0"]),
            OLD_DESCRIPTION,
            "поле не вернулось к опубликованному значению",
        )
        # (3) Другое поле не откатилось.
        self.assertEqual(
            _field_value(h, draft.id, bricks["f1"]),
            NEW_SUBTITLE,
            "отмена откатила другое поле",
        )
        # (5) Опубликованное сообщение не повреждено и не переопубликовано.
        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_text, after_subtitle)
        self.assertEqual(h.bot.content_text(h.chat_id, message_id), after_subtitle)
        self.assertEqual(h.posts(), posts_before, "отмена сделала публикацию")
        # (2)(4) Список полей актуален, экран ошибки закрыт.
        self.assertFalse(h.live_callbacks("anq_cancel:"), "экран ошибки остался")
        labels = " | ".join(label for label, _data in _field_buttons(h))
        self.assertIn("Подзаголовок", labels)
        self.assertIn("Описание — заполнено", labels, labels)
        self.assertEqual(_created_since(h, start), [], "отмена создала сообщение")
        self.assertEqual(
            list(_control_keyboards(h)), [message_id], "второе место управления"
        )

    @scenario_test
    async def test_e2_repeated_and_forged_cancel_is_safe(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=0)
        active = _only_active(h, draft.id)
        await _open_description(h, active.id)
        await h.text(LONG_TEXT)
        cancel = h.live_callbacks("anq_cancel:")[0]
        values_before = _value_rows(h, draft.id)
        text_before = _stored(h, draft.id).publication_text

        # Подделки чужого/неверного адреса ничего не пишут.
        brick = bricks["f0"]
        revision_part = cancel.rsplit(":", 1)[0]
        forged = [
            (revision_part + ":999", None),
            (revision_part + ":abc", None),
            (revision_part, None),
            (f"anq_cancel:999:{brick}:10", None),
            (f"anq_cancel:1:{bricks['f1']}:10", None),
            (cancel, 999),
        ]
        for data, user in forged:
            start = len(h.calls)
            kwargs = {"user_id": user} if user else {}
            await h.callback(data, **kwargs)
            self.assertEqual(
                _value_rows(h, draft.id), values_before, f"{data}: данные изменены"
            )
            self.assertEqual(
                _stored(h, draft.id).publication_text,
                text_before,
                f"{data}: анонс изменён",
            )
            self.assertEqual(_created_since(h, start), [], f"{data}: создано сообщение")
            self.assertTrue(
                h.live_callbacks("anq_cancel:"), f"{data}: экран ошибки потерян"
            )

        # Настоящая отмена срабатывает, повтор — уже нет.
        await h.callback(cancel)
        self.assertEqual(
            _field_value(h, draft.id, bricks["f0"]),
            OLD_DESCRIPTION,
            "настоящая отмена не сработала",
        )
        after_cancel = _value_rows(h, draft.id)
        start = len(h.calls)
        await h.callback(cancel)
        self.assertEqual(
            _value_rows(h, draft.id), after_cancel, "повтор отмены изменил данные"
        )
        self.assertEqual(
            _stored(h, draft.id).publication_text, text_before, "анонс изменён"
        )
        self.assertEqual(_created_since(h, start), [], "повтор создал сообщение")
        self.assertTrue(h.answer_texts()[-1], "клиент остался без ответа")

    @scenario_test
    async def test_e3_late_cancel_after_success_keeps_new_value(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h, photos=0)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)
        await _open_description(h, active.id)
        await h.text(LONG_TEXT)
        cancel = h.live_callbacks("anq_cancel:")[0]

        # Успешное сохранение нового значения.
        await h.text(FIXED_DESCRIPTION)
        stored_after_fix = _stored(h, draft.id).publication_text
        self.assertIn(FIXED_DESCRIPTION, stored_after_fix)

        # Позднее нажатие СТАРОЙ отмены ничего не откатывает.
        start = len(h.calls)
        await h.callback(cancel)
        self.assertIn(
            FIXED_DESCRIPTION,
            _stored(h, draft.id).publication_text,
            "поздняя отмена откатила новое значение",
        )
        self.assertEqual(h.bot.content_text(h.chat_id, message_id), stored_after_fix)
        self.assertTrue(h.answer_texts()[-1], "клиент остался без ответа")
        labels = " | ".join(label for label, _data in _field_buttons(h))
        self.assertIn("Описание", labels, "список полей пропал")
        self.assertEqual(_created_since(h, start), [], "создано сообщение")

    @scenario_test
    async def test_e4_cancel_after_restart_restores_published_value(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=0)
        active = _only_active(h, draft.id)
        await _open_description(h, active.id)
        await h.text(LONG_TEXT)
        cancel = h.live_callbacks("anq_cancel:")[0]

        fresh = await _restart_with_new_harness(h)
        try:
            await fresh.callback(cancel)
            self.assertEqual(
                _field_value(fresh, draft.id, bricks["f0"]),
                OLD_DESCRIPTION,
                "после рестарта отмена не вернула значение",
            )
            self.assertTrue(_field_buttons(fresh), "список полей не вернулся")
            self.assertFalse(fresh.live_callbacks("anq_cancel:"))
        finally:
            await fresh.close()

    @scenario_test
    async def test_e5_cancel_returns_last_published_not_first_value(self, h) -> None:
        """«Отменить» возвращает последнее ОПУБЛИКОВАННОЕ значение поля."""
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=0)
        active = _only_active(h, draft.id)

        # Успешная правка публикует «Среднее описание».
        await _open_subtitle(h, active.id)
        await h.text(NEW_SUBTITLE)
        await _open_description(h, active.id)
        await h.text(MIDDLE_DESCRIPTION)
        published = _stored(h, draft.id).publication_text
        self.assertIn(MIDDLE_DESCRIPTION, published)

        # Теперь непоместившийся текст и отмена.
        await _open_description(h, active.id)
        await h.text(LONG_TEXT)
        await h.callback(h.live_callbacks("anq_cancel:")[0])

        self.assertEqual(
            _field_value(h, draft.id, bricks["f0"]),
            MIDDLE_DESCRIPTION,
            "отмена вернула не последнее опубликованное значение",
        )
        self.assertEqual(_stored(h, draft.id).publication_text, published)
        self.assertIn(NEW_SUBTITLE, _stored(h, draft.id).publication_text)

    @scenario_test
    async def test_e6_foreign_user_cannot_edit_or_cancel(self, h) -> None:
        """Чужой пользователь не открывает экран, не пишет и не отменяет."""
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=0)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)
        published = _stored(h, draft.id).publication_text
        await _open_description(h, active.id)
        await h.text(LONG_TEXT)
        cancel = h.live_callbacks("anq_cancel:")[0]
        values_before = _value_rows(h, draft.id)

        for data in (
            f"anp_data:{active.id}",
            f"anf_sel:{active.id}:{bricks['f0']}",
            cancel,
        ):
            start = len(h.calls)
            await h.callback(data, user_id=555)
            self.assertEqual(
                _value_rows(h, draft.id), values_before, f"{data}: данные изменены"
            )
            self.assertEqual(_created_since(h, start), [], f"{data}: сообщение")
        await h.text(LONG_TEXT, user_id=555)

        self.assertEqual(_value_rows(h, draft.id), values_before)
        self.assertEqual(
            _stored(h, draft.id).publication_text, published, "анонс переписан"
        )
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), published, "пост изменён"
        )
        # Экран ошибки владельца цел, владелец может отменить.
        _assert_error_keyboard(self, h, message_id)
        await h.callback(cancel)
        self.assertEqual(
            _field_value(h, draft.id, bricks["f0"]),
            OLD_DESCRIPTION,
            "владелец не смог отменить правку",
        )


# --- Битые данные ----------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class BrokenStateTests(unittest.TestCase):
    """Битое состояние не теряет публикацию и не роняет ввод."""

    @scenario_test
    async def test_broken_payload_still_shows_error_and_saves_input(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=0)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)
        published = _stored(h, draft.id).publication_text
        await _open_description(h, active.id)

        # Повреждённое состояние пользователя (payload не JSON).
        with h.session_factory() as session:
            session.execute(
                text("UPDATE user_states SET payload = :p"), {"p": "not-json{"}
            )
            session.commit()

        await h.text(LONG_TEXT)

        self.assertEqual(
            _field_value(h, draft.id, bricks["f0"]), LONG_TEXT, "ввод потерян"
        )
        self.assertEqual(
            _stored(h, draft.id).publication_text, published, "анонс переписан"
        )
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), published, "пост изменён"
        )
        self.assertTrue(_limit_notices(h, TEXT_LIMIT), "причина не показана")
        self.assertEqual(len(_control_keyboards(h)), 1, _control_keyboards(h))

    @scenario_test
    async def test_broken_cancel_snapshot_does_not_rollback(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=0)
        active = _only_active(h, draft.id)
        await _open_description(h, active.id)
        await h.text(LONG_TEXT)
        cancel = h.live_callbacks("anq_cancel:")[0]
        texts_before = _stored(h, draft.id).publication_text
        values_before = _value_rows(h, draft.id)

        # Снимок «до правки» испорчен: восстановить нечего.
        with h.session_factory() as session:
            rows = session.execute(
                text("SELECT user_id, payload FROM user_states")
            ).all()
            for user_id, raw in rows:
                data = json.loads(raw)
                for value in data.values():
                    if isinstance(value, dict) and "brick_id" in value:
                        value["value_json"] = "{broken"
                session.execute(
                    text("UPDATE user_states SET payload = :p WHERE user_id = :u"),
                    {"p": json.dumps(data), "u": user_id},
                )
            session.commit()

        start = len(h.calls)
        await h.callback(cancel)

        # Ни откатов, ни падений: данные и публикация прежние.
        self.assertEqual(_value_rows(h, draft.id), values_before)
        self.assertEqual(_stored(h, draft.id).publication_text, texts_before)
        self.assertEqual(_created_since(h, start), [], "создано сообщение")
        self.assertTrue(h.answer_texts()[-1], "клиент остался без ответа")


if __name__ == "__main__":
    unittest.main()
