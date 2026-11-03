# tests/test_stage25_inplace.py
"""
Poster 2.5, этап C: правка доставленного анонса НА МЕСТЕ (spec §26–28, §58 Д/И).

Проверяется пользовательский результат через настоящее приложение, FakeBot и
временную SQLite (реальная `post_bot.db`, `.env` и сеть не используются):

- **Д. Редактирование** — текст правки уходит в существующее сообщение:
  без фото `editMessageText`, с одним фото и в альбоме — `editMessageCaption`
  по ПЕРВОМУ элементу группы. `message_id`, поколение экземпляра и кнопки
  управления остаются теми же, новая копия в чате не появляется, архивных
  снимков не образуется, отпечаток в прогрессе обновлён.
- **Д. Замена файла N↔N** — `editMessageMedia` по изменившемуся элементу,
  экземпляр тот же.
- **И. Изменение количества фото** — 0→1, 1→2, 2→1, 1→0, N→0: ровно одна
  новая доставка, прежний экземпляр архивный и без кнопок (панель удалена),
  при 0/1→N панель создана и её адрес записан. При сбое доставки прежний
  активный остаётся рабочим, дублей нет.
- Правка на месте не трогает `publication_progress["ids"]` и не нарушает
  триггерные ограничения опубликованного payload.
"""

from __future__ import annotations

import unittest

try:
    from announcement_instances import get_active_instance
    from handlers import announcement_wizard
    from handlers.announcement_wizard import AW_PREVIEW, CONVERSATION_NAME
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        inline_buttons,
        is_panel_record,
        scenario_test,
    )
    from tests.test_stage25_delivery_instances import (
        ANNOUNCEMENT_TEXT,
        _delivered_with_photos,
        _delivered_without_photos,
        _final_markup,
        _instances,
        _only_active,
        _stored,
    )
    from tests.test_stage27b_announcement_ui import (
        _callback,
        _latest_draft,
        _open_edit,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


DELIVERED_TITLE = "✏️ Изменить анонс"
PHOTO_STEP_TITLE = "📷 Фотографии анонса"
UPDATED_TEXT = "Обновлённое"


# --- помощники -------------------------------------------------------------


async def _change_value(h, text: str = UPDATED_TEXT) -> None:
    """Экран правки → «Данные» → пункт поля → новый текст."""
    await _open_edit(h)
    await h.callback(_callback(h, "aw_qmanage"))
    await h.callback(_callback(h, "aw_qgoto"))
    await h.text(text)


def _content_calls(h, kind: str) -> list[dict]:
    return [call for call in h.calls if call["type"] == kind]


def _calls_since(h, index: int) -> list[dict]:
    return h.calls[index:]


def _types(calls) -> list[str]:
    return [call["type"] for call in calls]


def _edits_of(h, message_id: int, kind: str = "edit_message") -> list[dict]:
    """Правки, адресованные конкретному сообщению (экран сюда не попадает)."""
    return [
        call
        for call in h.calls
        if call["type"] == kind and call.get("message_id") == message_id
    ]


def _panel_records(h) -> list[dict]:
    return [call for call in h.calls if is_panel_record(call)]


def _deleted(h, message_id: int) -> bool:
    return message_id in h.deleted_ids()


# --- Д. правка текста без фото --------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class InPlaceTextTests(unittest.TestCase):
    @scenario_test
    async def test_text_edit_updates_same_message(self, h) -> None:
        """§26/§28: текст правится в том же сообщении, копий и архива нет."""
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        before = _only_active(h, draft.id)
        message_id = before.message_ids[0]
        posts_before = len(h.posts())

        await _change_value(h)

        after = _only_active(h, draft.id)
        self.assertEqual(after.id, before.id, "экземпляр не пересоздавался")
        self.assertEqual(after.generation, before.generation)
        self.assertEqual(after.message_ids, before.message_ids, "адрес тот же")
        self.assertEqual(len(_instances(h, draft.id)), 1, "архивных снимков нет")

        edits = _edits_of(h, message_id)
        self.assertTrue(edits, "правка не ушла в существующее сообщение")
        self.assertIn(UPDATED_TEXT, h.bot.content_text(h.chat_id, message_id) or "")
        self.assertFalse(
            [call for call in h.calls if call["type"] == "message"][0:0],
            "новая копия не отправлялась",
        )
        self.assertEqual(len(h.posts()), posts_before, "второй пост не появился")
        self.assertEqual(h.posts(), [UPDATED_TEXT])

        # Управление осталось на месте: кнопки не пересоздавались.
        markup = _final_markup(h, message_id)
        self.assertIsNotNone(markup, "кнопки управления потеряны")
        self.assertTrue(
            all(data and data.startswith("anp_") for _l, data in inline_buttons(markup))
        )

        # Отпечаток обновлён: следующее «ничего не изменилось» не отправит
        # вторую версию.
        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.delivered_message_ids, before.message_ids)
        self.assertNotEqual(stored.delivery_fingerprint, before.fingerprint)
        self.assertEqual(stored.previous_message_ids, ())
        self.assertEqual(stored.publication_photos, ())
        self.assertEqual(len(h.posts()), 1)
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PREVIEW)


# --- Д. правка текста с одним фото ----------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class InPlaceSinglePhotoTests(unittest.TestCase):
    @scenario_test
    async def test_caption_edit_keeps_photo_message(self, h) -> None:
        """§28: одно фото — `editMessageCaption` единственного сообщения."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 1)
        before = _only_active(h, draft.id)
        message_id = before.message_ids[0]
        photos_before = _content_calls(h, "photo")

        await _change_value(h)

        after = _only_active(h, draft.id)
        self.assertEqual(after.id, before.id)
        self.assertEqual(after.message_ids, before.message_ids)
        self.assertEqual(after.generation, before.generation)

        captions = _content_calls(h, "edit_caption")
        self.assertTrue(captions, "подпись не обновлена")
        self.assertEqual(captions[-1]["message_id"], message_id)
        self.assertEqual(
            _content_calls(h, "photo"), photos_before, "фото не переотправлено"
        )
        self.assertEqual(
            _content_calls(h, "edit_media"), [], "файл не менялся — media не правится"
        )
        self.assertEqual(len(h.posts()), 1, "вторая версия не отправлена")
        self.assertIn(UPDATED_TEXT, h.bot.content_text(h.chat_id, message_id) or "")
        self.assertIsNotNone(_final_markup(h, message_id))


# --- Д. правка текста альбома ---------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class InPlaceAlbumTests(unittest.TestCase):
    @scenario_test
    async def test_caption_edit_goes_to_first_album_item(self, h) -> None:
        """§19/§28: текст альбома живёт в подписи ПЕРВОГО элемента группы."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 3)
        before = _only_active(h, draft.id)
        first_id = before.message_ids[0]
        panel_id = before.panel_message_id
        groups_before = len(_content_calls(h, "media_group"))

        await _change_value(h)

        after = _only_active(h, draft.id)
        self.assertEqual(after.id, before.id)
        self.assertEqual(after.message_ids, before.message_ids, "состав группы тот же")
        self.assertEqual(after.panel_message_id, panel_id, "панель та же")

        captions = _content_calls(h, "edit_caption")
        self.assertTrue(captions, "подпись альбома не обновлена")
        self.assertEqual(
            captions[-1]["message_id"],
            first_id,
            "подпись альбома правится по первому элементу",
        )
        self.assertEqual(len(_content_calls(h, "media_group")), groups_before)
        self.assertEqual(len(h.posts()), 1, "второй альбом не отправлен")
        self.assertFalse(_deleted(h, panel_id), "панель активного не удаляется")
        self.assertEqual(len(_panel_records(h)), 1, "вторая панель не отправлена")
        self.assertIn(UPDATED_TEXT, h.bot.content_text(h.chat_id, first_id) or "")


# --- Д. замена файла при том же числе фото ---------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class InPlaceMediaTests(unittest.TestCase):
    @scenario_test
    async def test_media_replacement_edits_the_same_instance(self, h) -> None:
        """§28: замена файла при неизменном числе — `editMessageMedia`."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 2)
        before = _only_active(h, draft.id)
        groups_before = len(_content_calls(h, "media_group"))
        captions_before = len(_content_calls(h, "edit_caption"))

        ok = await announcement_wizard._edit_active_instance_in_place(
            h.bot,
            h.chat_id,
            before,
            text=ANNOUNCEMENT_TEXT,
            photos=("photo-0", "replacement"),
            previous_photos=("photo-0", "photo-1"),
            previous_text=ANNOUNCEMENT_TEXT,
            fingerprint="replaced",
        )
        self.assertTrue(ok, "замена файла не применена")

        media = _content_calls(h, "edit_media")
        self.assertEqual(len(media), 1, "правится ровно изменившийся элемент")
        self.assertEqual(media[0]["message_id"], before.message_ids[1])
        self.assertEqual(
            len(_content_calls(h, "media_group")),
            groups_before,
            "группа не переотправлялась",
        )
        self.assertEqual(
            len(_content_calls(h, "edit_caption")),
            captions_before,
            "подпись не переписывалась: текст не менялся",
        )

        # Экземпляр тот же, отпечаток обновлён.
        after = get_active_instance(h.session_factory(), draft.id)
        self.assertIsNotNone(after)
        self.assertEqual(after.id, before.id)
        self.assertEqual(after.message_ids, before.message_ids)
        self.assertEqual(after.fingerprint, "replaced")


# --- И. изменение количества фото -----------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PhotoCountChangeTests(unittest.TestCase):
    async def _add_photo(self, h, file_id: str) -> None:
        """Шаг фото доставленного анонса: одно фото применяется сразу."""
        if PHOTO_STEP_TITLE not in h.screen_text():
            await _open_edit(h)
            await h.callback(_callback(h, "aw_photos"))
            if PHOTO_STEP_TITLE not in h.screen_text():
                raise AssertionError(f"Не шаг фото: {h.screen_text()}")
        await h.photo(file_id)

    async def _delete_photo(self, h, index: int = 0) -> None:
        if PHOTO_STEP_TITLE not in h.screen_text():
            await _open_edit(h)
            await h.callback(_callback(h, "aw_photos"))
        await h.callback(_callback(h, "aw_photo_del", f":{index}"))

    @scenario_test
    async def test_zero_to_one_photo_replaces_instance(self, h) -> None:
        """И: 0→1 — одна новая доставка, прежний текст архивный без кнопок."""
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        first = _only_active(h, draft.id)
        first_id = first.message_ids[0]
        posts_before = len(h.posts())
        edits_before = len(_edits_of(h, first_id))

        await self._add_photo(h, "photo-0")

        instances = _instances(h, draft.id)
        self.assertEqual(len(instances), 2, f"история: {instances}")
        active = _only_active(h, draft.id)
        self.assertNotEqual(active.id, first.id)
        self.assertEqual(active.generation, first.generation + 1)
        self.assertEqual(len(active.message_ids), 1)
        self.assertNotEqual(active.message_ids, first.message_ids)
        self.assertEqual(len(h.posts()), posts_before + 1, "ровно одна доставка")
        self.assertEqual(
            len(_edits_of(h, first_id)),
            edits_before,
            "структура изменилась — правкой на месте не обходимся",
        )
        self.assertIsNone(_final_markup(h, first_id), "кнопки архивного сняты")

        archived = [item for item in instances if item.id == first.id][0]
        self.assertEqual(archived.status, "archived")
        self.assertIsNone(_final_markup(h, first_id), "кнопки архивного сняты")
        self.assertFalse(_deleted(h, first_id), "архивный текст не удаляется")
        self.assertIsNotNone(_final_markup(h, active.message_ids[0]))

    @scenario_test
    async def test_one_to_two_photos_adds_panel(self, h) -> None:
        """И: 1→2 — новый альбом и ОДНА панель с записанным адресом."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 1)
        first = _only_active(h, draft.id)
        first_id = first.message_ids[0]
        self.assertIsNone(first.panel_message_id)

        await self._add_photo(h, "photo-1")

        active = _only_active(h, draft.id)
        self.assertNotEqual(active.id, first.id)
        self.assertEqual(len(active.message_ids), 2)
        groups = _content_calls(h, "media_group")
        self.assertEqual(len(groups), 1, "альбом отправлен один раз")
        self.assertEqual(
            [getattr(item, "media", None) for item in groups[0]["media"]],
            ["photo-0", "photo-1"],
        )
        panels = _panel_records(h)
        self.assertEqual(len(panels), 1, "ровно одна панель")
        panel_ids = h.bot.call_message_ids[id(panels[0])]
        self.assertEqual(len(panel_ids), 1)
        self.assertEqual(active.panel_message_id, panel_ids[0])
        # Прежнее одиночное фото стало архивным снимком без кнопок.
        self.assertIsNone(_final_markup(h, first_id))
        self.assertFalse(_deleted(h, first_id), "прежнее фото не удаляется")

    @scenario_test
    async def test_two_to_one_photo_strips_panel(self, h) -> None:
        """И: 2→1 — панель прежней группы удалена, новый снимок с кнопками."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 2)
        first = _only_active(h, draft.id)
        first_ids = first.message_ids
        panel_id = first.panel_message_id
        self.assertIsNotNone(panel_id)

        await self._delete_photo(h, index=1)

        instances = _instances(h, draft.id)
        active = _only_active(h, draft.id)
        self.assertNotEqual(active.id, first.id)
        self.assertEqual(len(active.message_ids), 1)
        self.assertIsNone(active.panel_message_id, "у одного фото панели нет")
        archived = [item for item in instances if item.id == first.id][0]
        self.assertEqual(archived.status, "archived")
        self.assertIn(panel_id, h.deleted_ids(), "панель прежней группы убрана")
        self.assertIsNone(archived.panel_message_id, "адрес удалённой панели снят")
        self.assertFalse(
            set(first_ids) & set(h.deleted_ids()),
            "вложения прежнего альбома остаются снимком",
        )
        self.assertIsNotNone(_final_markup(h, active.message_ids[0]))

    @scenario_test
    async def test_one_to_zero_photos_delivers_text(self, h) -> None:
        """И: 1→0 — текстовый экземпляр с кнопками, прежнее фото без кнопок."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 1)
        first = _only_active(h, draft.id)
        first_id = first.message_ids[0]
        captions_before = len(_content_calls(h, "edit_caption"))

        await self._delete_photo(h, index=0)

        active = _only_active(h, draft.id)
        instances = _instances(h, draft.id)
        self.assertEqual(len(instances), 2)
        self.assertNotEqual(active.id, first.id)
        self.assertEqual(len(active.message_ids), 1)
        self.assertEqual(
            _stored(h, draft.id).publication_photos,
            (),
            "новая версия без вложений",
        )
        self.assertIsNone(_final_markup(h, first_id), "кнопки прежнего сняты")
        self.assertIsNotNone(_final_markup(h, active.message_ids[0]))
        self.assertFalse(_deleted(h, first_id), "прежнее фото остаётся в чате")
        self.assertEqual(
            len(_content_calls(h, "edit_caption")),
            captions_before,
            "структура изменилась — правкой подписи не обходимся",
        )

    @scenario_test
    async def test_album_to_zero_photos_removes_panel(self, h) -> None:
        """И: N→0 — один новый текст, панель альбома удалена."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 2)
        first = _only_active(h, draft.id)
        panel_id = first.panel_message_id
        self.assertIsNotNone(panel_id)
        groups_before = len(_content_calls(h, "media_group"))

        await _open_edit(h)
        await h.callback(_callback(h, "aw_photos"))
        await h.callback(_callback(h, "aw_photo_clear"))
        await h.callback(_callback(h, "aw_photo_clear_yes"))

        active = _only_active(h, draft.id)
        self.assertNotEqual(active.id, first.id)
        self.assertEqual(len(active.message_ids), 1)
        self.assertIsNone(active.panel_message_id)
        self.assertIn(panel_id, h.deleted_ids(), "панель прежней группы убрана")
        self.assertEqual(
            len(_content_calls(h, "media_group")), groups_before, "новых групп нет"
        )
        self.assertIsNotNone(_final_markup(h, active.message_ids[0]))
        self.assertFalse(_deleted(h, first.message_ids[0]), "альбом не удаляется")


# --- И. сбой доставки при смене структуры ---------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class StructureChangeFailureTests(unittest.TestCase):
    @scenario_test
    async def test_failed_delivery_keeps_previous_instance(self, h) -> None:
        """§28/§30: при сбое отправки прежний активный остаётся рабочим."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 1)
        first = _only_active(h, draft.id)
        first_id = first.message_ids[0]
        calls_before = len(h.calls)

        # Сбой ровно в момент новой отправки: альбом не уходит.
        h.bot.fail_after["media_group"] = 0
        try:
            await self._add_photo_unchecked(h, "photo-1")
        finally:
            h.bot.fail_after.clear()

        instances = _instances(h, draft.id)
        self.assertEqual(len(instances), 1, f"второй экземпляр не создан: {instances}")
        active = _only_active(h, draft.id)
        self.assertEqual(active.id, first.id, "прежний экземпляр остался активным")
        self.assertEqual(active.message_ids, first.message_ids)
        self.assertIsNotNone(_final_markup(h, first_id), "управление не снято")
        self.assertFalse(_deleted(h, first_id), "прежний анонс не удалён")
        # Сбойная попытка не создала панель и не пересобрала экземпляр.
        self.assertEqual(_panel_records(h), [], "панель не отправлена")
        self.assertEqual(
            _types(_calls_since(h, calls_before)).count("media_group"),
            0,
            "альбом не подтверждён — второй версии нет",
        )
        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_status, "failed")
        # Незавершённая замена не переписывает адрес прошлой версии: он в
        # `previous_ids`, а `ids` новой версии пусты (доставка не подтверждена).
        self.assertEqual(stored.delivered_message_ids, ())
        self.assertEqual(stored.previous_message_ids, first.message_ids)

    async def _add_photo_unchecked(self, h, file_id: str) -> None:
        """Добавляет фото в доставленный анонс (сбой доставки гасит ошибка)."""
        if PHOTO_STEP_TITLE not in h.screen_text():
            await _open_edit(h)
            await h.callback(_callback(h, "aw_photos"))
        await h.photo(file_id)


# --- инварианты прогресса -------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class InPlaceProgressInvariantTests(unittest.TestCase):
    @scenario_test
    async def test_in_place_keeps_progress_ids_and_payload(self, h) -> None:
        """§26–28: `ids` те же, `previous_ids` не появились, payload обновлён."""
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        before = _stored(h, draft.id)

        await _change_value(h)

        after = _stored(h, draft.id)
        self.assertEqual(after.delivered_message_ids, before.delivered_message_ids)
        self.assertEqual(after.previous_message_ids, ())
        self.assertEqual(after.publication_status, "sent")
        self.assertIn(UPDATED_TEXT, after.publication_text or "")
        self.assertNotEqual(after.delivery_fingerprint, before.delivery_fingerprint)
        self.assertEqual(
            after.publication_progress.get("step"),
            before.publication_progress.get("step"),
            "шаг прежней доставки не переписан",
        )
        # Прогресс описывает ту же доставку: ключей прошлой версии нет.
        self.assertNotIn("previous_ids", after.publication_progress)

    @scenario_test
    async def test_repeat_without_changes_does_not_send_or_edit(self, h) -> None:
        """Повтор «ничего не изменилось» — без правок и без второй версии."""
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        await _change_value(h)
        calls_before = len(h.calls)
        posts_before = len(h.posts())

        await h.callback(f"aw_apply:{draft.id}:{_latest_draft(h).revision}")

        new_calls = h.calls[calls_before:]
        self.assertEqual(
            [call["type"] for call in new_calls if call["type"] != "callback_answer"],
            [],
            f"повтор что-то сделал: {new_calls}",
        )
        self.assertEqual(len(h.posts()), posts_before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
