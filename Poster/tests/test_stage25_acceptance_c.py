# tests/test_stage25_acceptance_c.py
"""
Poster 2.5, независимая приёмка этапа C (анонсы) — Tester.

Проверяется утверждённое пользовательское поведение (spec §18–37, §58
сценарии А–И), а не реализация Developer. Сценарии идут от того, что видит
автор в чате: сколько сообщений появилось, где живут кнопки, что происходит
со старым сообщением, что отвечает устаревшая кнопка.

Покрытие соседних наборов (чтобы не дублировать) осознанно оставлено там:
`test_stage25_delivery_instances` — А–Г на уровне delivery/прогресса,
`test_stage25_panel` — Е/Ж/З и маршрутизация панели,
`test_stage25_inplace` — Д/И и сбои замены структуры.

Здесь проверяются пробелы приёмки:

1. §19–20/§21/§22 — цельность на уровне ЧАТА: 0 фото — одно сообщение,
   1 — одно `sendPhoto` с подписью, 3 — один альбом с текстом в подписи
   ПЕРВОГО элемента и ровно одна панель.
2. §18/§21 — строка «Анонс готов» не уходит ни в одной ветке: публикация,
   переиздание, правка значения, смена структуры.
3. §29–30 — переоткрытие альбома: каждый раз новый экземпляр, прежний —
   снимок без кнопок, панель убрана, контент не удалён; история не растёт
   в новых логических анонсах.
4. §30 — правка значения через панель альбома и через клавиатуру 0-фото:
   тот же `message_id`, копий нет, кнопки продолжают работать.
5. §31/§28 — смена структуры 0→2 и N→0: новый альбом с панелью / новый
   текст с кнопками, прежние сообщения остались без кнопок; партия 2 фото к
   0-фото даёт РОВНО одну замену и один архивный снимок (§58 И).
6. §32–33 (Ж) — устаревшая кнопка после рестарта: только alert, ноль
   изменений чата и БД; кнопка активного экземпляра работает.
7. §34–37 — раскладки экрана фото (0/1/≥2), подтверждение «Удалить все»,
   пересчёт после каждого удаления, «◀️ Назад» к анонсу.
8. §58 Г — сбой доставки при смене структуры и «Продолжить отправку»:
   контент не дублируется, активный экземпляр ровно один.
9. §58 Д — инварианты правки на месте: `generation`/`message_ids` стабильны,
   отпечаток обновлён, клавиатура жива, повторной правки-дубля нет.
10. §27 — набор действий управления одинаков у сообщения (0/1 фото) и у
    панели (3 фото), счётчик фото соответствует коллекции.

FakeBot не отражает `editMessageText` в `h.posts()` для анонса без фото,
поэтому текст проверяется через `h.bot.content_text()` /
`h.current_post_ids()` — как и предупреждает задание.

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage25_acceptance_c -v
"""

from __future__ import annotations

import asyncio
import re
import unittest

try:
    from sqlalchemy import func, select

    import announcement_controls as controls
    from announcement_instances import list_instances
    from handlers.announcement_wizard import AW_PREVIEW, CONVERSATION_NAME
    from models import AnnouncementDraft
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        inline_buttons,
        scenario_test,
    )
    from tests.test_stage25_delivery_instances import (
        _delivered_with_photos,
        _delivered_without_photos,
        _final_markup,
        _instances,
        _only_active,
        _panel_records,
        _panel_sends,
        _stored,
        _text_copies,
    )
    from tests.test_stage27b_announcement_ui import _callback

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


# Пользовательские заголовки экранов (§18/§26–27/§34).
# 2.5.3 §5: экрана «✏️ Изменить анонс» больше нет — вместо него список
# текстовых полей на самом управлении анонса.
FIELDS_TITLE = "📝 Текстовые поля"
EXTRAS_TITLE = "🧩 Дополнительные поля"
MAIN_MENU_TITLE = "Здравствуйте! 👋"

# §18: экрана «Анонс готов» больше нет — ни как экран, ни как сообщение.
READY_SCREEN = "Анонс готов"

# §34–37 / 2.5.3 §6: подписи фото-режима. Кнопки «📥 Добавить фото» больше
# нет — снимки приходят в чат, а раскладка зависит от числа фото.
REMOVED_PHOTO_ADD = "📥 Добавить фото"
PHOTO_MODE_EMPTY = "📷 Пришлите фото"
PHOTO_MODE_MORE = "📷 Можно прислать ещё"
PHOTO_DELETE_ONE = "🗑 Удалить фото"
PHOTO_DELETE_ALL = "🗑 Удалить все фото"
PHOTO_CONFIRM = "🗑 Да, удалить все"
BACK = "◀️ Назад"

# §32: ответ устаревшей кнопке.
STALE_TEXT = controls.STALE_INSTANCE_TEXT

# §27/§18: действия редактора одинаковы у любой структуры медиа.
EXPECTED_ACTIONS = (
    ("📝 Текстовые поля", "data"),
    ("🖼 Фото (n)", "photos"),
    ("🧩 Дополнительные поля", "extras"),
    ("🏠 В меню", "menu"),
)

# Типы вызовов, которых не должно быть у устаревшей кнопки (§32).
FORBIDDEN_STALE_CALLS = frozenset(
    {
        "message",
        "photo",
        "media_group",
        "edit_message",
        "edit_caption",
        "edit_media",
        "edit_markup",
        "delete_message",
    }
)


# --- помощники -------------------------------------------------------------


def _labels(markup) -> list[list[str]]:
    """Подписи клавиатуры по строкам."""
    if markup is None or not hasattr(markup, "inline_keyboard"):
        return []
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _photo_layout(h, instance) -> list[list[str]]:
    """
    Живая раскладка фото-режима доставленного анонса (§6, 2.5.3).

    0–1 фото — клавиатура самого сообщения анонса (`editMessageReplyMarkup`),
    2+ — единственной панели (`editMessageText`). Отдельного экрана у режима
    нет, поэтому `screen()` здесь не подходит.
    """
    if instance.panel_message_id is not None:
        edits = [
            call
            for call in _panel_records(h)
            if call["type"] == "edit_message"
            and int(call.get("message_id") or 0) == int(instance.panel_message_id)
        ]
        return _labels(edits[-1].get("reply_markup")) if edits else []
    return _labels(_final_markup(h, instance.message_ids[0]))


def _panel_edits(h, instance) -> int:
    """Сколько раз правила текст панели: вторая панель исключена по счёту."""
    if instance.panel_message_id is None:
        return 0
    return len(
        [
            call
            for call in _panel_records(h)
            if call["type"] == "edit_message"
            and int(call.get("message_id") or 0) == int(instance.panel_message_id)
        ]
    )


def _button(h, prefix: str, suffix: str | None = None) -> str:
    for _label, data in h.screen_buttons():
        if (
            data
            and data.startswith(prefix)
            and (suffix is None or data.endswith(suffix))
        ):
            return data
    raise AssertionError(f"нет кнопки {prefix!r}{suffix or ''}: {h.screen_buttons()}")


def _live_button(h, prefix: str, suffix: str | None = None) -> str:
    """Callback живой кнопки чата (`live_buttons`), а не служебного экрана."""
    for _label, data in h.live_buttons():
        if (
            data
            and data.startswith(prefix)
            and (suffix is None or data.endswith(suffix))
        ):
            return data
    raise AssertionError(f"нет живой кнопки {prefix!r}{suffix or ''}: {h.live_buttons()}")


def _control_photos(h) -> str:
    """
    «🖼 Фото» управления анонсом.

    2.5.2 §2: после публикации служебного экрана нет, поэтому вход в экран
    фотографий — кнопка самого анонса (или его панели), а не экрана.
    2.5.3 §5: если открыт список текстовых полей, кнопки экземпляра скрыты —
    сначала «◀️ Назад» списка.
    """
    data = h.control_callback("photos")
    if data is None:
        raise AssertionError(
            f"управление анонсом не найдено: экран {h.screen_text()[:200]!r}"
        )
    return data


def _control_data(h) -> str:
    """«📝 Текстовые поля» управления анонсом (2.5.3 §5)."""
    data = h.control_callback("data")
    if data is None:
        raise AssertionError(
            f"управление анонсом не найдено: экран {h.screen_text()[:200]!r}"
        )
    return data


async def _open_field_question(h) -> None:
    """
    Открывает вопрос по первому текстовому полю (2.5.3 §5).

    Список полей заменяет клавиатуру анонса, поэтому второй шаг — нажатие
    `anf_sel:` из живых кнопок чата, а не из `screen_buttons`.
    """
    await h.callback(_control_data(h))
    for data in h.live_callbacks("anf_sel:"):
        await h.callback(data)
        return
    raise AssertionError(f"нет кнопок полей: {h.live_buttons()}")


async def _back_to_controls(h) -> None:
    """«◀️ Назад» списка полей возвращает управление анонсу (2.5.3 §5)."""
    for data in h.live_callbacks("anf_back:"):
        await h.callback(data)
        return
    raise AssertionError(f"нет «Назад» списка полей: {h.live_buttons()}")


def _assert_field_list(h) -> None:
    """
    В чате показан список текстовых полей (2.5.3 §5).

    Отдельного экрана нет: список заменяет управление анонса (0–1 фото) или
    текст его единственной панели (2+), поэтому проверяются живые клавиатуры
    чата, а не `screen_text`.
    """
    if not h.live_callbacks("anf_back:"):
        raise AssertionError(f"нет списка полей: {h.live_buttons()}")
    if h.live_callbacks("anp_"):
        raise AssertionError(
            f"управление анонса не скрыто: {h.live_control_callbacks()}"
        )


def _control_markup(h, instance):
    """
    Клавиатура управления экземпляром так, как её видит автор в чате.

    0/1 фото — на самом сообщении анонса (`editMessageReplyMarkup`), 2+ —
    на единственной служебной панели (§22).
    """
    if instance.panel_message_id is not None:
        for record in _panel_records(h):
            ids = h.bot.call_message_ids.get(id(record), ())
            if instance.panel_message_id in ids:
                return record.get("reply_markup")
        raise AssertionError(f"панель {instance.panel_message_id} не найдена")
    return _final_markup(h, instance.message_ids[0])


def _action_of(data: str | None) -> str:
    return (data or "").partition(":")[0]


def _norm_labels(markup) -> list[tuple[str, str]]:
    """(нормализованная подпись, действие): счётчик фото заменяется на (n)."""
    return [
        (re.sub(r"\(\d+\)", "(n)", label), _action_of(data))
        for label, data in inline_buttons(markup)
    ]


def _draft_count(h) -> int:
    with h.session_factory() as session:
        return int(session.scalar(select(func.count()).select_from(AnnouncementDraft)))


def _instance_rows(h, draft_id: int):
    return [
        (item.id, item.status, item.generation, item.message_ids)
        for item in list_instances(h.session_factory(), draft_id)
    ]


def _all_labels(labels: list[list[str]]) -> str:
    """Склейка подписей: проверка отсутствия/наличия кнопки."""
    return " | ".join(label for row in labels for label in row)


def _outgoing_texts(h) -> list[str]:
    """
    Всё, что бот отправил/показал как сообщение, подпись или экран.

    Проверка §18 идёт по фактическому чату: `sendMessage`/`sendPhoto`/
    `sendMediaGroup`/правки сообщений и экрана.
    """
    texts: list[str] = []
    for call in h.calls:
        kind = call["type"]
        if kind in {"message", "edit_message"}:
            texts.append(str(call.get("text") or ""))
        elif kind == "photo":
            texts.append(str(call.get("caption") or ""))
        elif kind == "media_group":
            texts.extend(
                str(getattr(item, "caption", None) or "") for item in call["media"]
            )
    return texts


async def _edit_value(h, text: str) -> None:
    """Управление анонса → «📝 Текстовые поля» → поле → новое значение."""
    draft_id = _only_active(h, _draft_id(h)).draft_id
    active = _only_active(h, draft_id)
    await h.callback(_panel_data(h, active))
    # 2.5.3 §5: список полей на самом управлении, затем ровно один вопрос.
    for data in h.live_callbacks("anf_sel:"):
        await h.callback(data)
        break
    else:
        raise AssertionError(f"нет кнопок полей: {h.live_buttons()}")
    await h.text(text)


def _draft_id(h) -> int:
    with h.session_factory() as session:
        return int(
            session.scalar(
                select(AnnouncementDraft.id).order_by(AnnouncementDraft.id.desc())
            )
        )


def _panel_data(h, instance) -> str:
    """`callback_data` кнопки «Данные» управления конкретным экземпляром."""
    for _label, data in inline_buttons(_control_markup(h, instance)):
        if data and data.startswith("anp_data:"):
            return data
    raise AssertionError(
        f"нет кнопки «Данные»: {inline_buttons(_control_markup(h, instance))}"
    )


# --- 1. цельность анонса (§19–20, §21, §22) --------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class IntegrityTests(unittest.TestCase):
    """В чате ровно то, что требуют §19–22: одна цельная единица анонса."""

    @scenario_test
    async def test_zero_photos_is_one_message_with_buttons(self, h) -> None:
        """§20: один `sendMessage` с полным текстом и управлением на нём."""
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        stored = _stored(h, draft.id)
        active = _only_active(h, draft.id)

        self.assertEqual(len(h.post_records()), 1, "одна единица контента в чате")
        self.assertEqual(active.message_ids, stored.delivered_message_ids)
        message_id = active.message_ids[0]
        # Текст — в этом же сообщении (FakeBot: правка на месте, не новый send).
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), stored.publication_text
        )
        self.assertEqual(
            h.current_post_ids().get(message_id),
            stored.publication_text,
            "итоговый текст доставленного сообщения",
        )
        self.assertEqual(_text_copies(h, stored.publication_text or ""), [message_id])
        buttons = inline_buttons(_control_markup(h, active))
        self.assertEqual(
            [_action_of(data) for _l, data in buttons],
            [f"anp_{action}" for action in controls.PANEL_ACTIONS],
            "управление — на том же сообщении",
        )
        self.assertNotIn(READY_SCREEN, " ".join(_outgoing_texts(h)))

    @scenario_test
    async def test_one_photo_is_single_photo_with_caption(self, h) -> None:
        """§21: одно `sendPhoto`, текст в подписи, копий нет."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 1)
        stored = _stored(h, draft.id)
        active = _only_active(h, draft.id)

        photos = [call for call in h.calls if call["type"] == "photo"]
        self.assertEqual(len(photos), 1, "ровно одна отправка фото")
        self.assertEqual(photos[0]["caption"], stored.publication_text)
        self.assertFalse(
            [call for call in h.calls if call["type"] == "media_group"],
            "одно фото не уходит альбомом",
        )
        self.assertEqual(len(h.post_records()), 1)
        self.assertEqual(_text_copies(h, stored.publication_text or ""), [])
        self.assertEqual(
            [
                _action_of(data)
                for _l, data in inline_buttons(_control_markup(h, active))
            ],
            [f"anp_{action}" for action in controls.PANEL_ACTIONS],
        )
        self.assertNotIn(READY_SCREEN, " ".join(_outgoing_texts(h)))

    @scenario_test
    async def test_three_photos_caption_on_first_and_one_panel(self, h) -> None:
        """§22: один альбом, текст в подписи ПЕРВОГО элемента, одна панель."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 3)
        stored = _stored(h, draft.id)
        active = _only_active(h, draft.id)

        groups = [call for call in h.calls if call["type"] == "media_group"]
        self.assertEqual(len(groups), 1, "ровно один `sendMediaGroup`")
        captions = [getattr(item, "caption", None) for item in groups[0]["media"]]
        self.assertEqual(
            captions,
            [stored.publication_text, None, None],
            "текст — в подписи ПЕРВОГО элемента, не последнего",
        )
        # Отдельного сообщения с полным текстом нет (§22).
        self.assertEqual(_text_copies(h, stored.publication_text or ""), [])
        panels = _panel_records(h)
        self.assertEqual(len(panels), 1, "ровно одна панель управления")
        self.assertEqual(len(h.post_records()), 1, "одна единица контента — альбом")
        self.assertEqual(len(active.message_ids), 3)
        panel_ids = h.bot.call_message_ids[id(panels[0])]
        self.assertEqual(active.panel_message_id, panel_ids[0])
        panel_text = panels[0].get("text") or ""
        self.assertNotIn(stored.publication_text or "", panel_text)
        self.assertNotIn(READY_SCREEN, panel_text)
        self.assertNotIn(READY_SCREEN, " ".join(_outgoing_texts(h)))


# --- 2. «Анонс готов» не отправляется нигде --------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class NoReadyScreenTests(unittest.TestCase):
    """§18/§21: промежуточного экрана нет ни в публикации, ни в правке."""

    @scenario_test
    async def test_ready_screen_absent_after_publish_republish_and_edit(
        self, h
    ) -> None:
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        # 1) повторное открытие = переиздание
        await h.callback("main_menu")
        await h.callback("my_announcements")
        await h.callback(_callback(h, "aw_open"))
        # 2) правка значения на месте
        await _edit_value(h, "Обновлено")
        # 3) смена структуры 0 → 1 фото
        await h.callback(_control_photos(h))
        await h.photo("photo-0")
        active = _only_active(h, draft.id)
        self.assertEqual(len(active.message_ids), 1, "новая версия с одним фото")

        texts = _outgoing_texts(h)
        self.assertFalse(
            [text for text in texts if READY_SCREEN in text],
            "экран «Анонс готов» не должен отправляться",
        )
        # 2.5.2 §2: экрана «✏️ Изменить анонс» после доставки не появляется —
        # управление живёт на самом анонсе. Остаётся только рабочий экран шага,
        # с которого пользователь менял структуру.
        self.assertNotIn("✏️ Изменить анонс", h.screen_text())
        self.assertIsNotNone(h.control_callback("data"), "нет управления анонсом")
        self.assertNotIn(READY_SCREEN, h.screen_text())


# --- 3. повторное открытие (§29–30) ----------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReopenAlbumTests(unittest.TestCase):
    """§29–30: каждое открытие из «Мои анонсы» — новый цельный экземпляр."""

    @scenario_test
    async def test_each_reopen_archives_previous_album_snapshot(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_with_photos(h, 3)
        drafts_before = _draft_count(h)
        seen = [_only_active(h, draft.id)]
        posts_after_publish = len(h.posts())

        for step in range(2):
            await h.callback("main_menu")
            await h.callback("my_announcements")
            await h.callback(_callback(h, "aw_open"))
            active = _only_active(h, draft.id)
            self.assertNotEqual(active.id, seen[-1].id, "новый экземпляр")
            self.assertEqual(active.generation, seen[-1].generation + 1)
            self.assertEqual(len(active.message_ids), 3, "новый альбом целиком")
            self.assertIsNotNone(active.panel_message_id, "у нового альбома панель")
            self.assertFalse(
                set(active.message_ids) & set(seen[-1].message_ids),
                "контент прежнего экземпляра не переиспользуется",
            )
            seen.append(active)

            previous = [i for i in _instances(h, draft.id) if i.id == seen[-2].id][0]
            self.assertEqual(previous.status, "archived")
            self.assertEqual(
                previous.message_ids, seen[-2].message_ids, "снимок не переписан"
            )
            self.assertIsNone(
                previous.panel_message_id, "адрес удалённой панели не остаётся"
            )
            self.assertFalse(
                set(previous.message_ids) & set(h.deleted_ids()),
                "архивный снимок остаётся в чате",
            )
            self.assertIn(
                seen[-2].panel_message_id,
                h.deleted_ids(),
                "панель прежнего альбома убрана",
            )
            if step == 0:
                self.assertIsNone(
                    _final_markup(h, seen[-2].message_ids[0]),
                    "у архивного снимка снята клавиатура",
                )
        # Один активный, история из трёх снимков, новый анонс в БД не создан.
        self.assertEqual(len([i for i in _instances(h, draft.id) if i.is_active]), 1)
        self.assertEqual(len(_instances(h, draft.id)), 3)
        self.assertEqual(_draft_count(h), drafts_before, "новых анонсов в БД нет")
        self.assertEqual(len(h.posts()), posts_after_publish + 2, "две новые версии")
        self.assertNotIn(READY_SCREEN, " ".join(_outgoing_texts(h)))


# --- 4. правка на месте без копий (§30) ------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class InPlaceEditTests(unittest.TestCase):
    """§30: правка значения обновляет то же сообщение, копий нет."""

    @scenario_test
    async def test_album_value_edit_keeps_message_and_live_buttons(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_with_photos(h, 2)
        active = _only_active(h, draft.id)
        first_id = active.message_ids[0]
        panel_id = active.panel_message_id
        posts_before = len(h.posts())

        await _edit_value(h, "Обновлено")

        edited = _only_active(h, draft.id)
        self.assertEqual(edited.id, active.id, "экземпляр не пересоздавался")
        self.assertEqual(edited.generation, active.generation)
        self.assertEqual(edited.message_ids, active.message_ids, "адрес тот же")
        self.assertEqual(edited.panel_message_id, panel_id, "панель та же")
        self.assertEqual(len(_instances(h, draft.id)), 1, "архивных снимков нет")
        self.assertEqual(len([c for c in h.calls if c["type"] == "media_group"]), 1)
        self.assertEqual(len(h.posts()), posts_before, "второй копии нет")
        self.assertIn("Обновлено", h.bot.content_text(h.chat_id, first_id) or "")
        self.assertFalse(
            [
                c
                for c in h.calls
                if c["type"] == "edit_markup" and c.get("message_id") == first_id
            ],
            "кнопки активного экземпляра не снимались",
        )
        # Кнопки живы: панель открывает редактор и после правки.
        await h.callback(_panel_data(h, edited))
        _assert_field_list(h)
        self.assertEqual(_only_active(h, draft.id).id, active.id)

    @scenario_test
    async def test_text_value_edit_keeps_message_and_live_buttons(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        active = _only_active(h, draft.id)
        message_id = active.message_ids[0]

        await _edit_value(h, "Другое значение")

        edited = _only_active(h, draft.id)
        self.assertEqual(edited.id, active.id)
        self.assertEqual(edited.message_ids, (message_id,))
        self.assertEqual(h.bot.content_text(h.chat_id, message_id), "Другое значение")
        self.assertEqual(len(h.posts()), 1, "второй пост не появился")
        await h.callback(controls.panel_callback("photos", edited.id))
        # 2.5.3 §6: фото-режим — клавиатура самого анонса, отдельного экрана
        # нет; у 0 фото это «📷 Пришлите фото» и «◀️ Назад».
        self.assertEqual(
            _photo_layout(h, edited),
            [[PHOTO_MODE_EMPTY], [BACK]],
            "фото-режим не показан на анонсе",
        )
        self.assertIsNone(h.screen_message_id(), "служебного экрана нет")


# --- 5. смена структуры (§31, §28) ----------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class StructureChangeTests(unittest.TestCase):
    """§31/§28: смена структуры — новый экземпляр, старый остаётся снимком."""

    @scenario_test
    async def test_zero_to_two_photos_replaces_instance(self, h) -> None:
        """И: 0→2 — альбом с панелью, прежний текст остаётся без кнопок."""
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        first = _only_active(h, draft.id)
        first_id = first.message_ids[0]

        await h.callback(_control_photos(h))
        await h.photo("p0", media_group_id="album")
        await h.photo("p1", media_group_id="album")
        await asyncio.sleep(0.4)  # альбом закрывается по паузе приёма

        active = _only_active(h, draft.id)
        self.assertNotEqual(active.id, first.id, "активный экземпляр заменён")
        self.assertEqual(len(active.message_ids), 2, "новый альбом целиком")
        self.assertIsNotNone(active.panel_message_id, "у альбома есть панель")
        groups = [c for c in h.calls if c["type"] == "media_group"]
        self.assertEqual(len(groups), 1, "альбом отправлен один раз")
        self.assertEqual(
            [getattr(item, "media", None) for item in groups[0]["media"]],
            ["p0", "p1"],
            "оба фото сохранены",
        )
        # Старое сообщение осталось в чате без кнопок.
        self.assertNotIn(first_id, h.deleted_ids(), "прежний текст не удаляется")
        self.assertIsNone(_final_markup(h, first_id), "кнопки прежнего сняты")
        self.assertEqual(
            [i.id for i in _instances(h, draft.id) if i.is_active], [active.id]
        )
        self.assertIsNotNone(
            _final_markup(h, active.message_ids[0]) or active.panel_message_id
        )

    @scenario_test
    async def test_batch_of_two_photos_replaces_instance_once(self, h) -> None:
        """И: партия 2 фото к 0-фото — одна замена и один архивный снимок.

        Telegram присылает альбом несколькими Update'ами с общим
        `media_group_id`; ждать закрытия партии нужно и для доставленного
        анонса, иначе каждый снимок даёт свою промежуточную доставку
        (0→1→2) и пользователь видит мигание панели (§28, §58 И).
        """
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        first = _only_active(h, draft.id)
        first_id = first.message_ids[0]
        await h.callback(_control_photos(h))
        mark = len(h.calls)

        await h.photo("p0", media_group_id="alb")
        await h.photo("p1", media_group_id="alb")
        await asyncio.sleep(0.4)  # партия закрывается по паузе приёма

        new_calls = h.calls[mark:]
        albums = [c for c in new_calls if c["type"] == "media_group"]
        self.assertEqual(len(albums), 1, "альбом отправлен ровно один раз")
        self.assertEqual(
            [getattr(item, "media", None) for item in albums[0]["media"]],
            ["p0", "p1"],
            "в альбоме оба снимка партии",
        )
        self.assertEqual(
            [c for c in new_calls if c["type"] == "photo"],
            [],
            "промежуточного sendPhoto по первому снимку нет",
        )
        # Один архивный снимок и один активный экземпляр.
        instances = _instances(h, draft.id)
        self.assertEqual(len(instances), 2, f"история: {instances}")
        self.assertEqual(
            [item.id for item in instances if item.status == "archived"],
            [first.id],
            "прежняя версия ушла в архив один раз",
        )
        active = _only_active(h, draft.id)
        self.assertNotEqual(active.id, first.id)
        self.assertEqual(len(active.message_ids), 2, "новый альбом целиком")
        self.assertIsNone(_final_markup(h, first_id), "кнопки архивного сняты")
        # Панель создана один раз и записана в активный экземпляр. Правил
        # (`editMessageText`) после приёма партии не считается: 2.5.3 §6
        # фото-режим правит существующую панель, а не создаёт вторую.
        sends = _panel_sends(h)
        self.assertEqual(len(sends), 1, "панель создана один раз")
        self.assertEqual(
            active.panel_message_id,
            h.bot.call_message_ids[id(sends[0])][0],
        )
        # Финальный активный экземпляр содержит оба фото партии.
        self.assertEqual(
            _stored(h, draft.id).publication_photos,
            ("p0", "p1"),
            "активная версия несёт всю партию",
        )

    @scenario_test
    async def test_album_to_zero_photos_removes_panel(self, h) -> None:
        """И: N→0 — панель удалена, новый текстовый пост с кнопками."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 3)
        first = _only_active(h, draft.id)
        panel_id = first.panel_message_id

        await h.callback(_control_photos(h))
        await h.callback(_live_button(h, "aw_photo_clear"))
        await h.callback(_live_button(h, "aw_photo_clear_yes"))

        active = _only_active(h, draft.id)
        self.assertNotEqual(active.id, first.id)
        self.assertEqual(len(active.message_ids), 1, "новый текстовый анонс")
        self.assertIsNone(active.panel_message_id)
        self.assertIn(panel_id, h.deleted_ids(), "панель прежнего альбома убрана")
        self.assertFalse(
            set(first.message_ids) & set(h.deleted_ids()),
            "альбом остаётся архивным снимком",
        )
        message_id = active.message_ids[0]
        self.assertIsNotNone(_final_markup(h, message_id), "новый пост с кнопками")
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            _stored(h, draft.id).publication_text,
        )
        self.assertEqual(
            len([c for c in h.calls if c["type"] == "media_group"]),
            1,
            "второй альбом не отправлялся",
        )


# --- 6. устаревшая кнопка после рестарта (§32–33, Ж) -----------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class StaleAfterRestartTests(unittest.TestCase):
    """§32: защита старых callback работает и после перезапуска службы."""

    @scenario_test
    async def test_archived_button_after_restart_only_alerts(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_with_photos(h, 3)
        first = _only_active(h, draft.id)
        await h.callback("main_menu")
        await h.callback("my_announcements")
        await h.callback(_callback(h, "aw_open"))
        active = _only_active(h, draft.id)
        self.assertNotEqual(active.id, first.id)

        await h.restart()

        for action in controls.PANEL_ACTIONS:
            rows_before = _instance_rows(h, draft.id)
            screen_before = h.screen_text()
            posts_before = h.posts()
            deleted_before = h.deleted_ids()
            calls_before = len(h.calls)
            answers_before = len(h.answers())

            await h.callback(controls.panel_callback(action, first.id))

            new_calls = h.calls[calls_before:]
            self.assertEqual(
                [c["type"] for c in new_calls if c["type"] in FORBIDDEN_STALE_CALLS],
                [],
                f"{action}: устаревшая кнопка что-то сделала после рестарта",
            )
            answers = h.answers()[answers_before:]
            self.assertEqual(len(answers), 1, f"{action}: нужен ровно один ответ")
            self.assertTrue(answers[0].get("show_alert"), f"{action}: нужен alert")
            self.assertEqual(answers[0].get("text"), STALE_TEXT)
            self.assertEqual(h.screen_text(), screen_before)
            self.assertEqual(h.posts(), posts_before)
            self.assertEqual(h.deleted_ids(), deleted_before)
            self.assertEqual(_instance_rows(h, draft.id), rows_before)

        # Кнопка действующего экземпляра после рестарта работает: фото-режим
        # открывается на самом управлении (панель альбома), экземпляр тот же.
        await h.callback(controls.panel_callback("photos", active.id))
        self.assertEqual(_only_active(h, draft.id).id, active.id)
        layout = _photo_layout(h, active)
        self.assertTrue(layout, f"фото-режим не открылся: {h.live_buttons()}")
        self.assertNotIn(REMOVED_PHOTO_ADD, _all_labels(layout), "кнопки добавления нет")
        self.assertEqual(layout[-1], [BACK], "выход из режима — «◀️ Назад»")
        self.assertNotIn(READY_SCREEN, " ".join(_outgoing_texts(h)))

    @scenario_test
    async def test_unknown_button_after_restart_only_alerts(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        active = _only_active(h, draft.id)
        await h.restart()

        screen_before = h.screen_text()
        calls_before = len(h.calls)
        answers_before = len(h.answers())
        await h.callback(controls.panel_callback("data", 999_999))

        self.assertEqual(
            [
                c["type"]
                for c in h.calls[calls_before:]
                if c["type"] != "callback_answer"
            ],
            [],
            "несуществующий экземпляр не должен ничего менять",
        )
        answers = h.answers()[answers_before:]
        self.assertEqual(len(answers), 1)
        self.assertTrue(answers[0].get("show_alert"))
        self.assertEqual(answers[0].get("text"), STALE_TEXT)
        self.assertEqual(_only_active(h, draft.id).id, active.id)
        self.assertEqual(h.screen_text(), screen_before)


# --- 7. экран фотографий (§34–37) -----------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PhotoScreenLayoutTests(unittest.TestCase):
    """§34–37: раскладка зависит от числа фото и пересчитывается сразу."""

    @scenario_test
    async def test_photo_screen_layouts_change_with_photo_count(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_with_photos(h, 3)
        active = _only_active(h, draft.id)
        await h.callback(_control_photos(h))

        # §6 (2.5.3): фото-режим живёт на самом управлении, отдельного экрана
        # и кнопки «📥 Добавить фото» нет.
        self.assertNotIn(REMOVED_PHOTO_ADD, _all_labels(_photo_layout(h, active)))
        self.assertEqual(
            _photo_layout(h, active),
            [
                [f"{PHOTO_DELETE_ONE} 1", f"{PHOTO_DELETE_ONE} 2"],
                [f"{PHOTO_DELETE_ONE} 3"],
                [PHOTO_DELETE_ALL],
                [BACK],
            ],
            "§37: три фото — удаление по две в строке и «удалить все»",
        )
        self.assertNotIn(
            "🏠 В меню", _all_labels(_photo_layout(h, active)), "§34: меню нет"
        )

        # 3 → 2: раскладка пересчитана сразу (состав медиагруппы меняется —
        # версия заменяется, панель переезжает вместе с ней).
        await h.callback(_live_button(h, "aw_photo_del", ":2"))
        active = _only_active(h, draft.id)
        self.assertEqual(
            _photo_layout(h, active),
            [
                [f"{PHOTO_DELETE_ONE} 1", f"{PHOTO_DELETE_ONE} 2"],
                [PHOTO_DELETE_ALL],
                [BACK],
            ],
        )
        # 2 → 1: анонс переиздан одним фото — управление на самом сообщении
        # (§21), панели у него быть не может.
        await h.callback(_live_button(h, "aw_photo_del", ":1"))
        active = _only_active(h, draft.id)
        self.assertIsNone(active.panel_message_id, "у одного фото панели нет")
        self.assertEqual(
            _photo_layout(h, active), [[PHOTO_MODE_MORE, PHOTO_DELETE_ONE], [BACK]]
        )
        # 1 → 0: раскладка нуля (§6) — информационная подсказка и «Назад».
        await h.callback(_live_button(h, "aw_photo_del", ":0"))
        active = _only_active(h, draft.id)
        self.assertEqual(
            _photo_layout(h, active), [[PHOTO_MODE_EMPTY], [BACK]], "§35: ноль фото"
        )
        # Второго фото-интерфейса нет: подсказка и «Удалить» существуют ровно
        # в одной живой клавиатуре (архивные версии остались без кнопок).
        self.assertEqual(
            [
                label
                for label, _data in h.live_buttons()
                if label in {PHOTO_MODE_EMPTY, PHOTO_MODE_MORE}
            ],
            [PHOTO_MODE_EMPTY],
            "одна клавиатура фото-режима",
        )

        # §6: «Назад» возвращает управление самому анонсу — клавиатура снова
        # `anp_`, фото-режим закрыт.
        await h.callback(_live_button(h, "anf_back"))
        controls_back = h.live_control_callbacks()
        self.assertEqual(
            sorted(data.rsplit(":", 1)[0] for data in controls_back),
            [f"anp_{action}" for action in sorted(controls.PANEL_ACTIONS)],
            f"управление анонсом не вернулось: {h.live_buttons()}",
        )
        self.assertEqual(
            [
                data
                for _label, data in h.live_buttons()
                if data and (data.startswith("aw_photo_") or data.startswith("noop:"))
            ],
            [],
            "фото-режим закрыт",
        )

    @scenario_test
    async def test_delete_all_requires_confirmation(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_with_photos(h, 2)
        active = _only_active(h, draft.id)
        photos_before = _stored(h, draft.id).photos
        await h.callback(_control_photos(h))

        # §37: массовое удаление — только с подтверждением.
        await h.callback(_live_button(h, "aw_photo_clear"))
        self.assertIn(
            PHOTO_CONFIRM, _all_labels(_photo_layout(h, active)), "нужно подтверждение"
        )
        panel_text = [
            call.get("text")
            for call in _panel_records(h)
            if call["type"] == "edit_message"
        ][-1]
        self.assertIn("Удалить все фото", panel_text or "", "панель объясняет действие")
        # Отмена ничего не меняет: коллекция, анонс и кнопки на месте.
        await h.callback(_control_photos(h))
        self.assertEqual(_stored(h, draft.id).photos, photos_before)
        self.assertEqual(_only_active(h, draft.id).id, active.id)
        self.assertEqual(
            _photo_layout(h, active)[0],
            [f"{PHOTO_DELETE_ONE} 1", f"{PHOTO_DELETE_ONE} 2"],
        )

        await h.callback(_live_button(h, "aw_photo_clear"))
        await h.callback(_live_button(h, "aw_photo_clear_yes"))
        # 2→0 — новый экземпляр: управление на самом сообщении, раскладка нуля.
        cleared = _only_active(h, draft.id)
        self.assertIsNone(cleared.panel_message_id)
        self.assertEqual(
            _photo_layout(h, cleared),
            [[PHOTO_MODE_EMPTY], [BACK]],
            "после удаления — раскладка нуля",
        )
        self.assertEqual(_stored(h, draft.id).publication_photos, ())


# --- 8. сбой и восстановление (§58 Г) ------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FailedDeliveryRetryTests(unittest.TestCase):
    """§58 Г/§24: сбой смены структуры и повтор не создают дублей."""

    @scenario_test
    async def test_failed_album_delivery_then_retry_delivers_once(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_with_photos(h, 1)
        first = _only_active(h, draft.id)
        first_id = first.message_ids[0]

        h.bot.fail_after["media_group"] = 0
        await h.callback(_control_photos(h))
        await h.photo("photo-1")
        h.bot.fail_after.clear()

        # Сбой: нового экземпляра нет, управление прежнего не снято.
        self.assertEqual(len(_instances(h, draft.id)), 1)
        self.assertEqual(_only_active(h, draft.id).id, first.id)
        self.assertIsNotNone(_final_markup(h, first_id), "управление живым осталось")
        self.assertFalse(
            [c for c in h.calls if c["type"] == "media_group"],
            "альбом не подтверждён — второй версии нет",
        )
        self.assertEqual(_stored(h, draft.id).publication_status, "failed")

        await h.callback(_button(h, "aw_pub_resume"))

        active = _only_active(h, draft.id)
        self.assertEqual(len(active.message_ids), 2, "новый альбом после повтора")
        self.assertIsNotNone(active.panel_message_id)
        self.assertEqual(len(_instances(h, draft.id)), 2, "ровно одна замена")
        groups = [c for c in h.calls if c["type"] == "media_group"]
        self.assertEqual(len(groups), 1, "подтверждённый альбом отправлен один раз")
        self.assertEqual(len(_panel_sends(h)), 1, "панель не задублирована")
        self.assertEqual(len(set(active.message_ids)), 2, "адреса уникальны")
        self.assertNotIn(first_id, active.message_ids, "контент не переиспользован")
        self.assertIsNone(_final_markup(h, first_id), "кнопки прежнего сняты")
        self.assertNotIn(first_id, h.deleted_ids(), "прежний пост не удалён")
        self.assertEqual(
            [i.status for i in _instances(h, draft.id) if i.id == first.id],
            ["archived"],
        )
        self.assertEqual(_stored(h, draft.id).publication_status, "sent")


# --- 9. инварианты правки на месте (§58 Д) --------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class InPlaceInvariantTests(unittest.TestCase):
    """§58 Д: поколение и адреса стабильны, отпечаток обновлён, кнопки живы."""

    @scenario_test
    async def test_in_place_invariants_and_no_duplicate_on_repeat(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_with_photos(h, 1)
        before = _only_active(h, draft.id)
        before_stored = _stored(h, draft.id)

        await _edit_value(h, "Первое изменение")
        after = _only_active(h, draft.id)
        after_stored = _stored(h, draft.id)

        self.assertEqual(after.id, before.id, "тот же экземпляр")
        self.assertEqual(after.generation, before.generation, "поколение стабильно")
        self.assertEqual(after.message_ids, before.message_ids, "адреса стабильны")
        self.assertEqual(
            after_stored.delivered_message_ids, before_stored.delivered_message_ids
        )
        self.assertEqual(after_stored.previous_message_ids, ())
        self.assertNotEqual(
            after_stored.delivery_fingerprint,
            before_stored.delivery_fingerprint,
            "отпечаток обновлён",
        )
        self.assertEqual(after.fingerprint, after_stored.delivery_fingerprint)
        self.assertIn("Первое изменение", after_stored.publication_text or "")
        self.assertEqual(len(_instances(h, draft.id)), 1)
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PREVIEW)

        # Кнопки после правки живы и открывают редактор того же экземпляра:
        # 2.5.3 §5 — список полей, из него ровно один вопрос.
        await h.callback(_panel_data(h, after))
        _assert_field_list(h)
        for data in h.live_callbacks("anf_sel:"):
            await h.callback(data)
            break
        else:
            raise AssertionError(f"нет кнопок полей: {h.live_buttons()}")
        await h.text("Второе изменение")

        repeated = _only_active(h, draft.id)
        self.assertEqual(repeated.id, before.id)
        self.assertEqual(repeated.message_ids, before.message_ids)
        self.assertEqual(len(_instances(h, draft.id)), 1, "копий не появилось")
        self.assertEqual(
            h.bot.content_text(h.chat_id, repeated.message_ids[0]),
            "Второе изменение",
            "вторая правка тоже применилась на месте",
        )
        self.assertEqual(
            len([c for c in h.calls if c["type"] == "photo"]),
            1,
            "фото не переотправлялось",
        )


# --- 10. одинаковый набор действий (0/1/3 фото) ---------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ControlActionsParityTests(unittest.TestCase):
    """§27: панель альбома и клавиатура сообщения — одни и те же действия."""

    @scenario_test
    async def test_zero_photos_controls_match_panel(self, h) -> None:
        await h.text("/start")
        await self._assert_parity(h, await _delivered_without_photos(h), 0)

    @scenario_test
    async def test_one_photo_controls_match_panel(self, h) -> None:
        await h.text("/start")
        await self._assert_parity(h, await _delivered_with_photos(h, 1), 1)

    @scenario_test
    async def test_album_panel_controls_match(self, h) -> None:
        await h.text("/start")
        await self._assert_parity(h, await _delivered_with_photos(h, 3), 3)

    async def _assert_parity(self, h, draft, photos_count: int) -> None:
        active = _only_active(h, draft.id)
        markup = _control_markup(h, active)
        self.assertEqual(
            _norm_labels(markup),
            [(label, f"anp_{action}") for label, action in EXPECTED_ACTIONS],
            "одинаковый набор действий управления",
        )
        # Счётчик фото в подписи соответствует фактической коллекции.
        self.assertIn(f"({photos_count})", _all_labels(_labels(markup)))
        # Все кнопки привязаны к действующему экземпляру (§32).
        instance_suffixes = set()
        for _label, data in inline_buttons(markup):
            assert data is not None, "у кнопки управления нет callback_data"
            instance_suffixes.add(data.rsplit(":", 1)[1])
        self.assertEqual(instance_suffixes, {str(active.id)})

        screens = {}
        for action in controls.PANEL_ACTIONS:
            if action == controls.ACTION_MENU:
                # 2.5.2 §2: «🏠 В меню» архивирует экземпляр — проверяем его
                # последним, иначе действующего экземпляра уже нет.
                continue
            await h.callback(controls.panel_callback(action, active.id))
            if action == controls.ACTION_DATA:
                # 2.5.3 §5: список полей — не экран, а клавиатура анонса.
                # Проверяем его по живым кнопкам и возвращаем управление,
                # иначе следующие действия недоступны.
                self.assertTrue(
                    h.live_callbacks("anf_sel:"),
                    f"список полей не показан: {h.live_buttons()}",
                )
                self.assertIsNone(
                    h.screen_message_id(), "служебного экрана для полей нет"
                )
                await _back_to_controls(h)
                continue
            if action == controls.ACTION_PHOTOS:
                # 2.5.3 §6: фото-режим — не отдельный экран, а клавиатура
                # самого управления. Проверяем состояние коллекции и
                # возвращаемся «◀️ Назад» к управлению анонсом.
                self.assertIsNone(
                    h.screen_message_id(), "служебного экрана для фото нет"
                )
                self.assertEqual(
                    _only_active(h, draft.id).id,
                    active.id,
                    f"{action}: экземпляр не должен меняться",
                )
                layout = _photo_layout(h, _only_active(h, draft.id))
                self.assertTrue(layout, f"фото-режим не показан: {h.live_buttons()}")
                self.assertNotIn(
                    REMOVED_PHOTO_ADD, _all_labels(layout), "кнопки добавления нет"
                )
                await _back_to_controls(h)
                continue
            screens[action] = h.screen_text().splitlines()[0]
            self.assertEqual(
                _only_active(h, draft.id).id,
                active.id,
                f"{action}: экземпляр не должен меняться",
            )
        # Кнопки рабочие: экземпляр жив до выхода в меню.
        await h.callback(controls.panel_callback(controls.ACTION_MENU, active.id))
        screens[controls.ACTION_MENU] = h.screen_text().splitlines()[0]
        self.assertEqual(
            screens,
            {
                "extras": EXTRAS_TITLE,
                "menu": MAIN_MENU_TITLE,
            },
            "каждое действие ведёт на свой экран",
        )
        self.assertEqual(len(set(screens.values())), 2, "экраны не дублируются")
        self.assertNotIn(READY_SCREEN, " ".join(_outgoing_texts(h)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
