# tests/test_stage25_delivery_instances.py
"""
Poster 2.5, этап C2b: связка доставки и экземпляров готового анонса (§58).

Проверяется пользовательский результат, а не внутренние функции:

- **А. Без фото** — одно сообщение с полным анонсом, управление прикреплено
  к нему самому; экземпляр ровно один и активный.
- **Б. Одно фото** — один `sendPhoto` с подписью, управление у того же
  сообщения, отдельных текстовых копий нет.
- **В. Медиагруппа** — один `sendMediaGroup`, caption только у ПЕРВОГО
  элемента, одна отдельная панель управления, её адрес записан в экземпляр.
- **Г. Сбой посередине** — подтверждённые id берутся из прогресса, повтор не
  переотправляет доставленное, финально ровно один активный экземпляр.
- **Правка → переиздание** — новый активный экземпляр (generation + 1),
  прежний архивный, у прежнего сообщения снята клавиатура, его текст и фото
  остались в чате, `delete_message` по прежним id не вызывается (удаляется
  только панель медиагруппы).
- **Повторное открытие из «Мои анонсы»** — §29 требует новый полноценный
  экземпляр, но `aw_open` переводится в C2b-2/C3, поэтому сценарий помечен
  skip.

Тесты идут через настоящее приложение, FakeBot и временную SQLite:
реальная `post_bot.db`, `.env` и сеть Telegram не используются.
"""

from __future__ import annotations

import asyncio
import unittest

try:
    import delivery
    from announcement_instances import list_instances
    from handlers import announcement_wizard
    from handlers.announcement_wizard import AW_PREVIEW, CONVERSATION_NAME
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        inline_buttons,
        is_panel_record,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        _callback,
        _callbacks,
        _choose_personal,
        _latest_draft,
        _open_edit,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


PHOTO_STEP_TITLE = "📷 Фотографии анонса"
DELIVERED_TITLE = "✅ Анонс готов."
ANNOUNCEMENT_TEXT = "Значение"


# --- помощники -------------------------------------------------------------


def _instances(h, draft_id: int):
    with h.session_factory() as session:
        return list_instances(session, draft_id)


def _active_instances(h, draft_id: int):
    return [item for item in _instances(h, draft_id) if item.is_active]


def _only_active(h, draft_id: int):
    active = _active_instances(h, draft_id)
    if len(active) != 1:
        raise AssertionError(f"активных экземпляров {len(active)}: {active}")
    return active[0]


def _stored(h, draft_id: int):
    from announcement_store import get_announcement_draft
    from tests.integration_harness import DEFAULT_USER_ID

    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
        )


def _markup_calls(h, message_id: int) -> list:
    """Все `editMessageReplyMarkup`, адресованные сообщению, по порядку."""
    return [
        call
        for call in h.calls
        if call["type"] == "edit_markup" and call.get("message_id") == message_id
    ]


def _final_markup(h, message_id: int):
    """Последняя клавиатура, прикреплённая к сообщению (None — кнопок нет)."""
    calls = _markup_calls(h, message_id)
    return calls[-1].get("reply_markup") if calls else None


def _panel_records(h) -> list[dict]:
    return [call for call in h.calls if is_panel_record(call)]


def _text_copies(h, text: str) -> list[int]:
    """Текстовые сообщения в чате с ровно таким текстом (не подписи фото)."""
    deleted = set(h.deleted_ids())
    return sorted(
        int(message_id)
        for (_chat_id, message_id), content in h.bot._message_content.items()
        if int(message_id) not in deleted
        and content[0] == "text"
        and content[1] == text
    )


async def _draft_at_photo_step(h):
    """Личный шаблон → ответ на единственный кирпичик → шаг фото."""
    place_id, _alias_id = _seed_place(h)
    template_id, _revision, _bricks = _seed_personal_template(h)
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    await h.text(ANNOUNCEMENT_TEXT)
    if PHOTO_STEP_TITLE not in h.screen_text():
        raise AssertionError(f"Не шаг фото: {h.screen_text()}")
    return _latest_draft(h)


async def _delivered_with_photos(h, count: int):
    """Доводит анонс до готового с `count` фото (1 — sendPhoto, 2+ — альбом)."""
    draft = await _draft_at_photo_step(h)
    if count == 1:
        # Одиночное фото завершает анонс сразу.
        await h.photo("photo-0")
    else:
        for index in range(count):
            await h.photo(f"photo-{index}", media_group_id="album")
        # Альбом финализируется по паузе в приёме (как в настоящем Telegram).
        await asyncio.sleep(0.4)
    if not h.screen_text().startswith(DELIVERED_TITLE):
        raise AssertionError(f"Анонс не доставлен: {h.screen_text()}")
    return draft


async def _delivered_without_photos(h):
    draft = await _draft_at_photo_step(h)
    await h.callback(_callback(h, "aw_photo_none"))
    if not h.screen_text().startswith(DELIVERED_TITLE):
        raise AssertionError(f"Анонс не доставлен: {h.screen_text()}")
    return draft


# --- А. без фото -----------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DeliveryWithoutPhotosTests(unittest.TestCase):
    @scenario_test
    async def test_zero_photos_single_message_with_control(self, h) -> None:
        """§20: одно сообщение с текстом, управление — на нём же."""
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        stored = _stored(h, draft.id)
        content_ids = tuple(stored.delivered_message_ids)
        self.assertEqual(len(content_ids), 1, f"контент: {content_ids}")
        content_id = content_ids[0]

        # Ровно одна копия текста; отдельного сообщения-копии нет.
        self.assertEqual(h.posts(), [stored.publication_text])
        self.assertEqual(_text_copies(h, stored.publication_text or ""), [content_id])

        # Управление — у КОНТЕНТА, а не у служебного экрана.
        markup = _final_markup(h, content_id)
        self.assertIsNotNone(markup, "у анонса нет клавиатуры управления")
        buttons = inline_buttons(markup)
        self.assertTrue(buttons)
        self.assertTrue(
            all(data and data.startswith("anp_") for _label, data in buttons),
            buttons,
        )
        screen_markup = (h.screen() or {}).get("reply_markup")
        self.assertFalse(
            [
                data
                for _l, data in inline_buttons(screen_markup)
                if data and data.startswith("anp_")
            ],
            "кнопки управления не должны жить на служебном экране",
        )

        # Экземпляр ровно один, он активный и описывает доставленное сообщение.
        active = _only_active(h, draft.id)
        self.assertEqual(active.message_ids, content_ids)
        self.assertEqual(active.generation, 1)
        self.assertTrue(active.fingerprint)
        # Конструктор завершён: шага фото больше нет, состояние — готовый анонс.
        self.assertFalse(_callbacks(h, "aw_photo_none"))
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PREVIEW)


# --- Б. одно фото ----------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DeliverySinglePhotoTests(unittest.TestCase):
    @scenario_test
    async def test_one_photo_single_message_with_control(self, h) -> None:
        """§21: фото + весь текст caption; копий текста нет."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 1)
        stored = _stored(h, draft.id)
        content_ids = tuple(stored.delivered_message_ids)
        self.assertEqual(len(content_ids), 1, f"контент: {content_ids}")
        content_id = content_ids[0]

        photos = [call for call in h.calls if call["type"] == "photo"]
        self.assertEqual(len(photos), 1, "ровно одна отправка фото")
        self.assertEqual(photos[0]["caption"], stored.publication_text)
        self.assertFalse(
            [call for call in h.calls if call["type"] == "media_group"],
            "одиночное фото не уходит медиагруппой",
        )
        # Отдельных текстовых сообщений с анонсом нет: текст живёт в подписи.
        self.assertEqual(_text_copies(h, stored.publication_text or ""), [])
        self.assertEqual(h.posts(), [stored.publication_text])

        # Управление прикреплено к тому же сообщению (после регистрации
        # экземпляра: id нужен для callback).
        markup = _final_markup(h, content_id)
        self.assertIsNotNone(markup, "у фото-анонса нет клавиатуры управления")
        self.assertTrue(
            all(data and data.startswith("anp_") for _l, data in inline_buttons(markup))
        )
        active = _only_active(h, draft.id)
        self.assertEqual(active.message_ids, content_ids)
        self.assertEqual(active.generation, 1)


# --- В. медиагруппа --------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DeliveryAlbumTests(unittest.TestCase):
    @scenario_test
    async def test_three_photos_album_and_one_panel(self, h) -> None:
        """§22: один альбом, caption первого элемента, одна панель."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 3)
        stored = _stored(h, draft.id)
        content_ids = tuple(stored.delivered_message_ids)
        self.assertEqual(len(content_ids), 3, "id каждого вложения альбома")

        groups = [call for call in h.calls if call["type"] == "media_group"]
        self.assertEqual(len(groups), 1, "ровно одна медиагруппа")
        media = groups[0]["media"]
        self.assertEqual(
            [getattr(item, "media", None) for item in media],
            ["photo-0", "photo-1", "photo-2"],
        )
        self.assertEqual(
            [getattr(item, "caption", None) for item in media],
            [stored.publication_text, None, None],
            "§19/§22: весь текст — только в подписи ПЕРВОГО элемента",
        )
        # Отдельного сообщения с полным текстом нет.
        self.assertEqual(_text_copies(h, stored.publication_text or ""), [])
        self.assertEqual(h.posts(), [stored.publication_text])

        # Панель — одно служебное сообщение, текста анонса не дублирует.
        panels = _panel_records(h)
        self.assertEqual(len(panels), 1, f"панелей: {len(panels)}")
        panel = panels[0]
        self.assertEqual(panel["type"], "message")
        self.assertIsInstance(panel.get("text"), str)
        self.assertNotIn(stored.publication_text or "", panel.get("text") or "")
        self.assertNotIn("Анонс готов", panel.get("text") or "")
        panel_buttons = inline_buttons(panel.get("reply_markup"))
        self.assertTrue(panel_buttons)
        self.assertTrue(
            all(data and data.startswith("anp_") for _l, data in panel_buttons),
            panel_buttons,
        )

        active = _only_active(h, draft.id)
        self.assertEqual(active.message_ids, content_ids)
        panel_id = h.bot.call_message_ids[id(panel)]
        self.assertEqual(len(panel_id), 1)
        self.assertEqual(
            active.panel_message_id,
            panel_id[0],
            "адрес панели записан в активный экземпляр",
        )


# --- Г. сбой посередине ----------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DeliveryResumeTests(unittest.TestCase):
    @scenario_test
    async def test_failure_midway_retry_does_not_duplicate_content(self, h) -> None:
        """
        §24: подтверждённое до сбоя не отправляется повторно.

        Шаг фото в мастере ограничен 10 снимками, поэтому «середина» (группа
        10 + хвост) проверяется на реальной доставке и реальной БД: продукт
        решает, что уже доставлено, по `progress["ids"]`.
        """
        await h.text("/start")
        draft = await _draft_at_photo_step(h)
        photos = [f"mid-{index}" for index in range(11)]
        progress: dict = {}

        h.bot.fail_after["photo"] = 0
        with self.assertRaises(RuntimeError):
            await delivery.send_announcement_post(
                h.bot,
                h.chat_id,
                ANNOUNCEMENT_TEXT,
                photos,
                progress=progress,
            )
        # Группа подтверждена (10 id), падение хвоста в прогресс не попало.
        self.assertEqual(progress["step"], 1)
        self.assertEqual(len(progress["ids"]), 10)
        self.assertEqual(progress["total"], 2)

        h.bot.fail_after.clear()
        content_ids = await delivery.send_announcement_post(
            h.bot,
            h.chat_id,
            ANNOUNCEMENT_TEXT,
            photos,
            progress=progress,
        )
        self.assertEqual(len(content_ids), 11)
        self.assertEqual(progress["ids"], content_ids)
        # Подтверждённая группа не отправлена заново: только хвост.
        self.assertEqual(
            len([call for call in h.calls if call["type"] == "media_group"]),
            1,
        )
        tails = [call for call in h.calls if call["type"] == "photo"]
        self.assertEqual(len(tails), 1)
        self.assertIsNone(tails[0]["caption"], "текст уже ушёл в подписи группы")

        # Учёт экземпляра идемпотентен: повторный вызов не создаёт второй
        # активный экземпляр и не дублирует панель.
        for _attempt in range(2):
            await announcement_wizard._ensure_delivered_instance(
                h.bot,
                h.chat_id,
                draft.id,
                content_ids,
                photos,
                fingerprint="midway",
            )
        active = _only_active(h, draft.id)
        self.assertEqual(active.message_ids, tuple(content_ids))
        self.assertEqual(len(_instances(h, draft.id)), 1, "второй экземпляр не создан")
        self.assertEqual(len(_panel_records(h)), 1, "панель отправлена один раз")
        panel_id = h.bot.call_message_ids[id(_panel_records(h)[0])][0]
        self.assertEqual(active.panel_message_id, panel_id)


# --- правка → переиздание --------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class InstanceArchiveTests(unittest.TestCase):
    @scenario_test
    async def test_album_edit_archives_previous_and_removes_panel(self, h) -> None:
        """§30–31: новая версия активна, прежняя — архивный снимок без кнопок."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 3)
        first = _only_active(h, draft.id)
        first_ids = tuple(first.message_ids)
        first_panel = first.panel_message_id
        self.assertIsNotNone(first_panel)

        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.text("Обновлено")

        instances = _instances(h, draft.id)
        self.assertEqual(len(instances), 2, f"история: {instances}")
        active = _only_active(h, draft.id)
        self.assertNotEqual(active.id, first.id)
        self.assertEqual(active.generation, first.generation + 1)
        self.assertEqual(active.status, "active")
        self.assertEqual(len(active.message_ids), 3, "новый альбом целиком")
        self.assertFalse(set(active.message_ids) & set(first_ids))

        archived = [item for item in instances if item.id == first.id][0]
        self.assertEqual(archived.status, "archived")
        self.assertEqual(archived.message_ids, first_ids, "снимок не переписан")
        self.assertEqual(archived.panel_message_id, first_panel)

        # У прежнего сообщения снята клавиатура (панель удалена), а контент
        # остался: текст и фото в чате не трогаем.
        caption_id = delivery.caption_message_id(first_ids)
        self.assertIsNotNone(caption_id)
        self.assertIsNone(
            _final_markup(h, caption_id),
            "клавиатура прежнего экземпляра должна быть снята",
        )
        self.assertFalse(
            set(first_ids) & set(h.deleted_ids()),
            "текст/фото прежнего экземпляра остаются в чате",
        )
        self.assertIn(first_panel, h.deleted_ids(), "панель прежней версии убрана")
        # Обе версии видны как доставленные посты.
        self.assertEqual(h.posts(), [ANNOUNCEMENT_TEXT, "Обновлено"])

    @scenario_test
    async def test_text_edit_archives_previous_without_deleting_it(self, h) -> None:
        """§30–31: текстовый анонс остаётся снимком, удаляется только клавиатура."""
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        first = _only_active(h, draft.id)
        first_id = first.message_ids[0]

        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.text("Обновлено")

        instances = _instances(h, draft.id)
        self.assertEqual(len(instances), 2)
        active = _only_active(h, draft.id)
        self.assertEqual(active.generation, 2)
        archived = [item for item in instances if item.id == first.id][0]
        self.assertEqual(archived.status, "archived")
        self.assertEqual(archived.message_ids, (first_id,))

        self.assertIsNone(
            _final_markup(h, first_id),
            "клавиатура прежнего экземпляра снята",
        )
        self.assertNotIn(first_id, h.deleted_ids(), "прежний текст не удаляется")
        self.assertEqual(h.bot.content_text(h.chat_id, first_id), ANNOUNCEMENT_TEXT)
        # Управление нового экземпляра — у нового сообщения.
        new_markup = _final_markup(h, active.message_ids[0])
        self.assertIsNotNone(new_markup)
        self.assertTrue(
            all(
                data and data.startswith("anp_")
                for _l, data in inline_buttons(new_markup)
            )
        )
        self.assertEqual(h.posts(), [ANNOUNCEMENT_TEXT, "Обновлено"])


# --- повторное открытие ----------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReopenFromMyAnnouncementsTests(unittest.TestCase):
    @scenario_test
    @unittest.skip(
        "C2b-2/C3: aw_open ещё рендерит экран «Анонс готов» и не отправляет "
        "новый полноценный экземпляр (§29)."
    )
    async def test_reopen_sends_new_instance(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        first = _only_active(h, draft.id)

        await h.callback("main_menu")
        await h.callback("my_announcements")
        await h.callback(_callback(h, "aw_open"))

        active = _only_active(h, draft.id)
        self.assertNotEqual(active.id, first.id)
        self.assertEqual(active.generation, 2)
        self.assertEqual(
            [item for item in _instances(h, draft.id) if item.id == first.id][0].status,
            "archived",
        )
        self.assertIsNone(_final_markup(h, first.message_ids[0]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
