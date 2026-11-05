# tests/test_stage253_photos_e2.py
"""
Poster 2.5.3 §6 — приёмка E2: фото-режим живёт на самом анонсе.

Проверяется пользовательский результат через настоящее приложение, FakeBot и
временную SQLite (`tests/integration_harness.py`): реальная `post_bot.db`,
`.env` и сеть Telegram не используются.

Что закрепляется (утверждённый контракт §6, все сценарии — путь автора):

1. 0 фото: «🖼 Фото» НЕ создаёт сообщений — раскладка `[«📷 Пришлите фото»
   (noop), «◀️ Назад»]` приходит клавиатурой самого анонса
   (`editMessageReplyMarkup`), текст и подпись не меняются.
2. 1 фото: «📷 Можно прислать ещё» + «🗑 Удалить фото» в одной строке.
3. 2–10 фото: единственная панель медиагруппы правится
   (`editMessageText`), вторая панель не создаётся; удаление отдельных
   снимков + «🗑 Удалить все фото» + «◀️ Назад».
4. Кнопки «📥 Добавить фото» нет ни в одной живой клавиатуре: снимок
   приходит обычным сообщением, и после него режим не закрывается.
5. Одиночное фото и альбом применяются к доставленному анонсу сразу;
   альбом уходит ОДНОЙ медиагруппой.
6. Удаление одного снимка и удаление всех работает из фото-режима.
7. «◀️ Назад» (`anf_back:{instance_id}`) возвращает управление анонсу
   (`anp_*`) — второй интерфейс не остаётся.
8. Рестарт (новый Application на тех же FakeBot/БД): фото-режим
   открывается по `instance_id`.
9. Информационная кнопка (`noop:0:0`) не меняет ни сообщений, ни БД.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage253_photos_e2 -v
"""

from __future__ import annotations

import asyncio
import unittest

try:
    import announcement_controls as controls
    from handlers.announcement_wizard import AW_PREVIEW, CONVERSATION_NAME
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.test_stage25_delivery_instances import (
        _final_markup,
        _only_active,
        _panel_records,
        _panel_sends,
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
    from utils.ui_texts import tr

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


# Подписи — из каталога текстов (config/ui_texts.yaml), а не литералами.
PHOTOS_TITLE = tr("wizard.photos.title")
MODE_EMPTY = tr("wizard.photos.mode_empty")
MODE_MORE = tr("wizard.photos.mode_more")
DELETE_ONE = tr("buttons.wizard.udalit_edinstvennoe_foto")
DELETE_ALL = tr("buttons.wizard.udalit_vse_foto")
BACK = tr("buttons.nav.back")

# 2.5.3 §6: прежней кнопки добавления больше нет.
REMOVED_ADD = "📥 Добавить фото"

# Типы вызовов, создающих новое сообщение в чате.
CREATE_TYPES = frozenset({"message", "photo", "media_group", "rich_message"})

# Информационная кнопка фото-режима: `noop` — безопасное бездействие.
NOOP_CALLBACK = "noop:0:0"


# --- помощники -------------------------------------------------------------


def _created_count(h) -> int:
    """Сколько вызовов создали сообщения бота (без правок и удалений)."""
    return sum(1 for call in h.calls if call["type"] in CREATE_TYPES)


def _markup_labels(markup) -> list[list[str]]:
    """Раскладка клавиатуры по строкам (подписи)."""
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _layout(h, instance) -> list[list[str]]:
    """
    Живая раскладка фото-режима (§6).

    0–1 фото — клавиатура самого сообщения анонса (`editMessageReplyMarkup`),
    2+ — текст единственной панели (`editMessageText`).
    """
    if instance.panel_message_id is not None:
        edits = [
            call
            for call in _panel_records(h)
            if call["type"] == "edit_message"
            and int(call.get("message_id") or 0) == int(instance.panel_message_id)
        ]
        return _markup_labels(edits[-1].get("reply_markup")) if edits else []
    return _markup_labels(_final_markup(h, instance.message_ids[0]))


def _live_callback(h, prefix: str) -> str:
    """Callback живой кнопки чата с данным префиксом (§6)."""
    found = h.live_callbacks(prefix)
    if not found:
        raise AssertionError(f"нет живой кнопки {prefix!r}: {h.live_buttons()}")
    return found[0]


def _photo_mode_labels(h) -> list[str]:
    """Подписи живых кнопок чата, относящихся к фото-режиму."""
    return [
        label
        for label, data in h.live_buttons()
        if data and (data == NOOP_CALLBACK or data.startswith("aw_photo_"))
    ]


def _all_texts(h) -> str:
    """Всё, что бот показал как текст/подпись/экран (проверка отсутствия)."""
    out: list[str] = []
    for call in h.calls:
        kind = call["type"]
        if kind in {"message", "edit_message"}:
            out.append(str(call.get("text") or ""))
        elif kind == "photo":
            out.append(str(call.get("caption") or ""))
        elif kind == "media_group":
            for item in call.get("media") or ():
                out.append(str(getattr(item, "caption", None) or ""))
    return "\n".join(out)


def _panel_texts(h) -> list[str]:
    """Тексты сообщений панели управления (отправка и правки)."""
    return [
        str(call.get("text") or "")
        for call in _panel_records(h)
        if call["type"] in {"message", "edit_message"}
    ]


async def _delivered_personal(h, *, photos: int = 0, fields: int = 1):
    """Личный шаблон с `fields` полями и доставка с `photos` снимками."""
    place_id, _alias_id = _seed_place(h)
    specs: list[tuple[str, str, str, dict[str, object]]] = [
        (f"f{index}", f"Поле {index}", "short_text", {}) for index in range(fields)
    ]
    content = "\n".join(f"Строка {index}: [f{index}]" for index in range(fields))
    template_id, _revision, _bricks = _seed_personal_template(
        h, specs=specs, content=content
    )
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    for _ in range(fields + 5):
        if PHOTOS_TITLE in h.screen_text():
            break
        await h.text("Значение")
    if PHOTOS_TITLE not in h.screen_text():
        raise AssertionError(f"Не шаг фото: {h.screen_text()[:200]!r}")
    if photos == 0:
        await h.callback(_callback(h, "aw_photo_none"))
    elif photos == 1:
        await h.photo("photo-0")
    else:
        for index in range(photos):
            await h.photo(f"photo-{index}", media_group_id="album")
        await asyncio.sleep(0.4)
    draft = _latest_draft(h)
    if _stored(h, draft.id).publication_status != "sent":
        raise AssertionError(f"Анонс не доставлен: {h.screen_text()[:200]!r}")
    return draft


async def _open_photo_mode(h) -> None:
    """Пользовательский вход в фото-режим: «🖼 Фото» управления анонса."""
    data = h.control_callback("photos")
    if data is None:
        raise AssertionError(f"у анонса нет кнопки «Фото»: {h.live_buttons()}")
    await h.callback(data)


async def _back_to_controls(h) -> None:
    """«◀️ Назад» фото-режима возвращает управление анонсу (`anp_*`)."""
    await h.callback(_live_callback(h, "anf_back:"))


# --- 1. раскладка 0 фото ---------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ZeroPhotosModeTests(unittest.TestCase):
    """§6: 0 фото — подсказка и «Назад» на самом анонсе, без нового сообщения."""

    @scenario_test
    async def test_zero_photos_mode_lives_on_announcement(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_personal(h, photos=0)
        active = _only_active(h, draft.id)
        message_id = active.message_ids[0]
        text_before = h.bot.content_text(h.chat_id, message_id)
        stored_before = _stored(h, draft.id)
        created_before = _created_count(h)
        edits_before = len(h.calls)

        await _open_photo_mode(h)

        # Нового сообщения и служебного экрана нет: правится клавиатура анонса.
        self.assertEqual(_created_count(h), created_before, "сообщений не прибавилось")
        self.assertIsNone(h.screen_message_id(), "служебного экрана нет")
        self.assertEqual(
            _only_active(h, draft.id).id, active.id, "экземпляр не менялся"
        )
        kinds = {call["type"] for call in h.calls[edits_before:]}
        self.assertIn("edit_markup", kinds, "клавиатура анонса не правилась")
        self.assertNotIn("edit_message", kinds, "текст анонса не трогается")
        self.assertNotIn("edit_caption", kinds, "подпись анонса не трогается")

        # Раскладка 0 фото: информационная подсказка и «Назад».
        self.assertEqual(
            _layout(h, active),
            [[MODE_EMPTY], [BACK]],
            "§6: 0 фото — «📷 Пришлите фото» и «◀️ Назад»",
        )
        self.assertEqual(
            h.live_label(NOOP_CALLBACK), MODE_EMPTY, "подсказка — noop-кнопка"
        )
        self.assertEqual(
            h.live_callbacks("anf_back:"),
            [controls.field_callback(controls.FIELD_ACTION_BACK, active.id)],
            "«Назад» — живая кнопка anf_back:{instance_id}",
        )
        # «📥 Добавить фото» больше нет ни в интерфейсе, ни в текстах.
        self.assertNotIn(REMOVED_ADD, _photo_mode_labels(h))
        self.assertNotIn(REMOVED_ADD, _all_texts(h))

        # Ни текст, ни подпись, ни коллекция не изменились.
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), text_before, "текст анонса цел"
        )
        self.assertEqual(_stored(h, draft.id).photos, stored_before.photos)
        self.assertEqual(
            _stored(h, draft.id).delivered_message_ids,
            stored_before.delivered_message_ids,
        )
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PREVIEW)


# --- 2. раскладка 1 фото ---------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class SinglePhotoModeTests(unittest.TestCase):
    """§6: одно фото — «Можно прислать ещё» и «Удалить фото» в одной строке."""

    @scenario_test
    async def test_one_photo_mode_layout_and_caption_kept(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_personal(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = active.message_ids[0]
        caption_before = h.bot.content_text(h.chat_id, message_id)
        panels_before = len(_panel_sends(h))
        created_before = _created_count(h)

        await _open_photo_mode(h)

        self.assertEqual(_created_count(h), created_before, "новых сообщений нет")
        self.assertEqual(len(_panel_sends(h)), panels_before, "панель не создавалась")
        self.assertIsNone(h.screen_message_id(), "служебного экрана нет")
        self.assertIsNone(active.panel_message_id, "у одного фото панели нет")
        self.assertEqual(
            _layout(h, active),
            [[MODE_MORE, DELETE_ONE], [BACK]],
            "§6: одно фото — подсказка и удаление в одной строке",
        )
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            caption_before,
            "подпись анонса не переписана",
        )
        self.assertNotIn(
            DELETE_ALL, _photo_mode_labels(h), "«удалить все» у одного фото нет"
        )


# --- 3. раскладка 2+ фото --------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AlbumPhotosModeTests(unittest.TestCase):
    """§6: 2–10 фото — единственная панель, удаление и «Удалить все фото»."""

    @scenario_test
    async def test_album_mode_edits_existing_panel_only(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_personal(h, photos=3)
        active = _only_active(h, draft.id)
        panel_id = active.panel_message_id
        self.assertIsNotNone(panel_id, "у альбома нет панели")
        sends_before = len(_panel_sends(h))
        created_before = _created_count(h)

        await _open_photo_mode(h)

        self.assertEqual(_created_count(h), created_before, "второй панели нет")
        self.assertEqual(
            len(_panel_sends(h)), sends_before, "панель не отправлялась заново"
        )
        self.assertIsNone(h.screen_message_id(), "служебного экрана нет")
        self.assertEqual(
            _layout(h, active),
            [
                [
                    f"{tr('buttons.wizard.udalit_foto', value=1)}",
                    f"{tr('buttons.wizard.udalit_foto', value=2)}",
                ],
                [f"{tr('buttons.wizard.udalit_foto', value=3)}"],
                [DELETE_ALL],
                [BACK],
            ],
            "§6: три фото — удаление по две в строке и «удалить все»",
        )
        # Панель — единственный текст управления: ни копии анонса, ни экрана.
        self.assertTrue(
            _panel_texts(h)[0].startswith(controls.panel_text().splitlines()[0]),
            _panel_texts(h)[0],
        )
        self.assertNotIn(
            REMOVED_ADD, "\n".join(_panel_texts(h)), "кнопки добавления нет"
        )


# --- 4. приём снимков в режиме ---------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PhotoIntakeTests(unittest.TestCase):
    """§6: снимки приходят обычным сообщением, режим не закрывается."""

    @scenario_test
    async def test_single_photo_applies_and_stays_in_mode(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_personal(h, photos=0)
        first = _only_active(h, draft.id)
        await _open_photo_mode(h)

        await h.photo("new-photo")

        # Снимок применён сразу: доставлен новый экземпляр с одним фото.
        active = _only_active(h, draft.id)
        self.assertNotEqual(active.id, first.id, "0→1 — новый экземпляр")
        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.publication_photos, ("new-photo",))
        # §30–31: прежняя версия остаётся в чате архивным снимком, но
        # активная доставка — ровно одна и с новым фото.
        self.assertEqual(
            tuple(stored.previous_message_ids),
            tuple(first.message_ids),
            "прежняя версия стала архивной",
        )
        self.assertFalse(
            set(stored.delivered_message_ids) & set(first.message_ids),
            "id новой версии не смешиваются с прежними",
        )

        # Режим не закрылся: раскладка одного фото на новом анонсе.
        self.assertEqual(
            _layout(h, active),
            [[MODE_MORE, DELETE_ONE], [BACK]],
            "после снимка автор остаётся в фото-режиме",
        )
        self.assertEqual(
            h.live_callbacks("anf_back:"),
            [controls.field_callback(controls.FIELD_ACTION_BACK, active.id)],
            "«Назад» привязан к новому экземпляру",
        )
        # Ввод пользователя удалён: подтверждённая доставка состоялась.
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PREVIEW)

    @scenario_test
    async def test_album_is_sent_as_one_group(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_personal(h, photos=0)
        await _open_photo_mode(h)
        groups_before = len([call for call in h.calls if call["type"] == "media_group"])

        await h.photo("album-0", media_group_id="grp")
        await h.photo("album-1", media_group_id="grp")
        await asyncio.sleep(0.4)

        groups = [call for call in h.calls if call["type"] == "media_group"]
        self.assertEqual(len(groups), groups_before + 1, "альбом ушёл одной партией")
        self.assertEqual(
            [getattr(item, "media", None) for item in groups[-1]["media"]],
            ["album-0", "album-1"],
            "оба снимка в одной медиагруппе",
        )
        active = _only_active(h, draft.id)
        self.assertIsNotNone(active.panel_message_id, "у альбома появилась панель")
        self.assertEqual(
            _stored(h, draft.id).publication_photos, ("album-0", "album-1")
        )
        self.assertEqual(
            _layout(h, active),
            [
                [
                    f"{tr('buttons.wizard.udalit_foto', value=1)}",
                    f"{tr('buttons.wizard.udalit_foto', value=2)}",
                ],
                [DELETE_ALL],
                [BACK],
            ],
            "режим перешёл на раскладку альбома",
        )
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PREVIEW)


# --- 5. удаление снимков ---------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PhotoDeletionTests(unittest.TestCase):
    """§6: удаление одного снимка и всех — из фото-режима, сразу."""

    @scenario_test
    async def test_delete_one_photo_updates_announcement(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_personal(h, photos=2)
        first = _only_active(h, draft.id)
        await _open_photo_mode(h)

        await h.callback(_live_callback(h, "aw_photo_del:"))

        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.photos, ("photo-1",), "удалён первый снимок")
        self.assertEqual(stored.publication_photos, ("photo-1",))
        active = _only_active(h, draft.id)
        self.assertNotEqual(active.id, first.id, "2→1 — новый экземпляр")
        self.assertIsNone(active.panel_message_id, "у одного фото панели нет")
        self.assertEqual(
            _layout(h, active),
            [[MODE_MORE, DELETE_ONE], [BACK]],
            "раскладка пересчитана на одного фото",
        )
        # Прежняя версия осталась архивным снимком без кнопок.
        for message_id in first.message_ids:
            self.assertIsNone(_final_markup(h, message_id), "кнопки архивного сняты")

    @scenario_test
    async def test_delete_all_requires_confirmation_and_clears(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_personal(h, photos=2)
        first = _only_active(h, draft.id)
        await _open_photo_mode(h)

        await h.callback(_live_callback(h, "aw_photo_clear:"))
        # Подтверждение — в самой панели: второй интерфейс не появляется.
        self.assertIsNone(h.screen_message_id(), "служебного экрана нет")
        confirm = _live_callback(h, "aw_photo_clear_yes:")
        self.assertIn(
            tr("buttons.wizard.da_udalit_vse"),
            [label for label, data in h.live_buttons() if data == confirm],
            "нужно подтверждение удаления",
        )
        self.assertEqual(
            _stored(h, draft.id).photos, ("photo-0", "photo-1"), "пока целы"
        )

        await h.callback(confirm)

        stored = _stored(h, draft.id)
        self.assertEqual(stored.photos, (), "коллекция очищена")
        self.assertEqual(stored.publication_photos, ())
        self.assertEqual(stored.publication_status, "sent")
        active = _only_active(h, draft.id)
        self.assertNotEqual(active.id, first.id, "2→0 — новый экземпляр")
        self.assertEqual(
            _layout(h, active),
            [[MODE_EMPTY], [BACK]],
            "после удаления — раскладка нуля",
        )
        self.assertIn(first.panel_message_id, h.deleted_ids(), "панель альбома убрана")


# --- 6. выход из режима ----------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class BackFromPhotoModeTests(unittest.TestCase):
    """§6: «◀️ Назад» возвращает управление анонсу, режим закрыт."""

    @scenario_test
    async def test_back_restores_control_and_closes_mode(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_personal(h, photos=2)
        active = _only_active(h, draft.id)
        controls_before = h.live_control_callbacks()
        self.assertTrue(controls_before, "у анонса нет управления")

        await _open_photo_mode(h)
        self.assertTrue(_live_callback(h, "anf_back:"))

        await _back_to_controls(h)

        # Управление анонсом снова целиком — тот же набор `anp_`, что до входа.
        self.assertEqual(
            h.live_control_callbacks(),
            controls_before,
            f"управление анонсом не вернулось: {h.live_buttons()}",
        )
        self.assertEqual(
            [
                data
                for _label, data in h.live_buttons()
                if data and (data == NOOP_CALLBACK or data.startswith("aw_photo_"))
            ],
            [],
            "фото-режим закрыт",
        )
        self.assertEqual(_only_active(h, draft.id).id, active.id, "экземпляр тот же")
        self.assertEqual(
            _stored(h, draft.id).publication_photos, ("photo-0", "photo-1")
        )

    @scenario_test
    async def test_back_from_zero_photo_mode(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_personal(h, photos=0)
        active = _only_active(h, draft.id)
        controls_before = h.live_control_callbacks()
        self.assertTrue(controls_before, "у анонса нет управления")

        await _open_photo_mode(h)
        await _back_to_controls(h)

        self.assertEqual(
            h.live_control_callbacks(),
            controls_before,
            f"управление анонсом не вернулось: {h.live_buttons()}",
        )
        self.assertFalse(
            [label for label in _photo_mode_labels(h)],
            "кнопки фото-режима скрыты",
        )
        self.assertEqual(_only_active(h, draft.id).id, active.id)


# --- 7. рестарт -----------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PhotoModeRestartTests(unittest.TestCase):
    """§6: после рестарта фото-режим открывается по `instance_id`."""

    @scenario_test
    async def test_photo_mode_opens_after_restart(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_personal(h, photos=2)
        active = _only_active(h, draft.id)
        photos_callback = h.control_callback("photos")
        self.assertIsNotNone(photos_callback)

        await h.restart()

        # Ни user_data, ни состояния диалога после рестарта нет.
        self.assertIsNone(
            h.user_data().get("announcement_ui"), "состояние диалога потерялось"
        )
        await h.callback(photos_callback)

        self.assertIsNotNone(
            _layout(h, active), f"фото-режим не открылся: {h.live_buttons()}"
        )
        self.assertEqual(
            h.live_callbacks("aw_photo_del:"),
            [
                f"aw_photo_del:{draft.id}:{_stored(h, draft.id).revision}:{index}"
                for index in range(2)
            ],
            "удаление доступно после рестарта",
        )
        self.assertEqual(
            h.live_callbacks("anf_back:"),
            [controls.field_callback(controls.FIELD_ACTION_BACK, active.id)],
        )
        self.assertEqual(_only_active(h, draft.id).id, active.id, "экземпляр тот же")

        # Удаление после рестарта тоже применяется к доставленному анонсу.
        await h.callback(_live_callback(h, "aw_photo_del:"))
        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.publication_photos, ("photo-1",))


# --- 8. информационная кнопка ---------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class NoopButtonTests(unittest.TestCase):
    """§6: «📷 Пришлите фото» — подсказка, нажатие ничего не меняет."""

    @scenario_test
    async def test_noop_button_changes_nothing(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_personal(h, photos=0)
        active = _only_active(h, draft.id)
        await _open_photo_mode(h)

        stored_before = _stored(h, draft.id)
        created_before = _created_count(h)
        calls_before = len(h.calls)

        await h.callback(NOOP_CALLBACK)

        # Ни сообщений, ни правок, ни изменений в БД — только ответ (спиннер).
        self.assertEqual(_created_count(h), created_before, "новых сообщений нет")
        self.assertEqual(len(h.calls), calls_before + 1, "только answerCallbackQuery")
        self.assertEqual(h.calls[-1]["type"], "callback_answer")
        after = _stored(h, draft.id)
        self.assertEqual(after.photos, stored_before.photos)
        self.assertEqual(after.revision, stored_before.revision)
        self.assertEqual(
            after.delivered_message_ids, stored_before.delivered_message_ids
        )
        self.assertEqual(after.publication_status, "sent")
        self.assertEqual(_only_active(h, draft.id).id, active.id, "экземпляр тот же")
        # Режим остался открытым: подсказка и «Назад» на месте.
        self.assertEqual(
            _layout(h, active), [[MODE_EMPTY], [BACK]], "режим не закрылся"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
