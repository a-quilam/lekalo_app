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
    """
    Личный шаблон → ответ на единственный кирпичик → шаг фото.

    Poster 2.5.2 §4: кнопка «🧩 Дополнительные поля» показывается только
    при реальных кандидатах, поэтому общий фикстур сеет расширенную версию
    шаблона с доп. кирпичиком: экраны управления в этих тестах должны
    проверять полный набор действий, а не зависеть от пустого extras.
    Текст анонса при этом остаётся каноническим (`extras` не выбраны).
    """
    place_id, _alias_id = _seed_place(h)
    template_id, _revision, _bricks = _seed_personal_template(
        h,
        specs=[
            ("main", "Основное поле", "short_text", {}),
            ("extra", "Доп. поле", "short_text", {}),
        ],
        content="[main]",
        extended_content="[main]\n[extra]",
    )
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
    _assert_delivered(h, draft)
    return draft


async def _delivered_without_photos(h):
    draft = await _draft_at_photo_step(h)
    await h.callback(_callback(h, "aw_photo_none"))
    _assert_delivered(h, draft)
    return draft


def _assert_delivered(h, draft) -> None:
    """
    Анонс доставлен: `sent` в БД, служебного экрана в чате нет (2.5.2 §2).

    После публикации единственное место управления — сам анонс (или его
    панель), поэтому признаком успеха служит статус черновика, а не текст
    экрана: экран шага закрывается.
    """
    stored = _stored(h, draft.id)
    if stored.publication_status != "sent":
        raise AssertionError(
            f"Анонс не доставлен ({stored.publication_status}): {h.screen_text()}"
        )


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
        # Служебный экран закрыт (§2): второго места управления в чате нет.
        self.assertIsNone(h.screen_message_id(), "экран шага закрыт")
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
    async def test_album_edit_in_place_then_reopen_archives_previous(self, h) -> None:
        """
        §26–28 + §30–31: правка альбома идёт на месте, архив даёт переоткрытие.

        Пока структура медиа та же, текст правится в существующем альбоме
        (`editMessageCaption` по первому элементу): экземпляр, его поколение и
        панель не меняются. Архивный снимок появляется только там, где он и
        задуман, — при повторном открытии анонса из «Мои анонсы»: прежний
        экземпляр остаётся в чате без кнопок, панель группы убирается.
        """
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

        # Правка применена к тому же экземпляру: копий и архива нет.
        self.assertEqual(len(_instances(h, draft.id)), 1)
        edited = _only_active(h, draft.id)
        self.assertEqual(edited.id, first.id)
        self.assertEqual(edited.generation, first.generation)
        self.assertEqual(edited.message_ids, first_ids)
        self.assertEqual(edited.panel_message_id, first_panel)
        caption_id = delivery.caption_message_id(first_ids)
        assert caption_id is not None
        self.assertEqual(
            h.bot.content_text(h.chat_id, caption_id),
            "Обновлено",
            "подпись альбома обновлена на месте",
        )
        self.assertEqual(
            len([call for call in h.calls if call["type"] == "media_group"]),
            1,
            "второй альбом не отправлен",
        )
        # В записи `sendMediaGroup` остаётся подпись на момент отправки, а в
        # чате она уже обновлена правкой на месте (проверено выше).
        self.assertEqual(h.posts(), [ANNOUNCEMENT_TEXT])

        # §29–30: переоткрытие из «Мои анонсы» отправляет новый экземпляр.
        await h.callback("main_menu")
        await h.callback("my_announcements")
        await h.callback(_callback(h, "aw_open"))

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
        # §31: снимок сохраняет текст, фото и состав вложений; адрес панели,
        # которой в чате больше нет, не остаётся живой ссылкой.
        self.assertIsNone(archived.panel_message_id)
        # Правка на месте обновила отпечаток: снимок описывает то, что
        # реально осталось в чате (подпись «Обновлено»), а не версию до неё.
        self.assertEqual(archived.fingerprint, edited.fingerprint)

        # У прежнего сообщения снята клавиатура, а контент остался: текст и
        # фото в чате не трогаем. Панель прежней группы удалена.
        self.assertIsNone(
            _final_markup(h, caption_id),
            "клавиатура прежнего экземпляра должна быть снята",
        )
        self.assertFalse(
            set(first_ids) & set(h.deleted_ids()),
            "текст/фото прежнего экземпляра остаются в чате",
        )
        self.assertIn(first_panel, h.deleted_ids(), "панель прежней версии убрана")
        self.assertEqual(
            h.posts(),
            [ANNOUNCEMENT_TEXT, "Обновлено"],
            "обе версии видны как доставленные посты",
        )

    @scenario_test
    async def test_text_edit_in_place_then_reopen_archives_previous(self, h) -> None:
        """§28 + §30–31: текст правится на месте, архив — при переоткрытии."""
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        first = _only_active(h, draft.id)
        first_id = first.message_ids[0]

        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.text("Обновлено")

        self.assertEqual(len(_instances(h, draft.id)), 1, "правка не плодит экземпляры")
        edited = _only_active(h, draft.id)
        self.assertEqual(edited.id, first.id)
        self.assertEqual(edited.message_ids, (first_id,))
        self.assertEqual(h.bot.content_text(h.chat_id, first_id), "Обновлено")
        self.assertIsNotNone(_final_markup(h, first_id))

        await h.callback("main_menu")
        await h.callback("my_announcements")
        await h.callback(_callback(h, "aw_open"))

        instances = _instances(h, draft.id)
        self.assertEqual(len(instances), 2)
        active = _only_active(h, draft.id)
        self.assertNotEqual(active.id, first.id)
        self.assertEqual(active.generation, first.generation + 1)
        archived = [item for item in instances if item.id == first.id][0]
        self.assertEqual(archived.status, "archived")
        self.assertEqual(archived.message_ids, (first_id,))
        # Правка на месте обновила отпечаток: снимок описывает то, что
        # реально осталось в чате (текст «Обновлено»), а не версию до правки.
        self.assertEqual(archived.fingerprint, edited.fingerprint)

        self.assertIsNone(
            _final_markup(h, first_id),
            "клавиатура прежнего экземпляра снята",
        )
        self.assertNotIn(first_id, h.deleted_ids(), "прежний текст не удаляется")
        self.assertEqual(
            h.bot.content_text(h.chat_id, first_id),
            "Обновлено",
            "архивный снимок сохраняет свой текст",
        )
        # Управление нового экземпляра — у нового сообщения.
        new_markup = _final_markup(h, active.message_ids[0])
        self.assertIsNotNone(new_markup)
        self.assertTrue(
            all(
                data and data.startswith("anp_")
                for _l, data in inline_buttons(new_markup)
            )
        )
        self.assertNotEqual(active.message_ids, first.message_ids)
        self.assertEqual(h.posts(), ["Обновлено", "Обновлено"])


# --- повторное открытие ----------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReopenFromMyAnnouncementsTests(unittest.TestCase):
    @scenario_test
    async def test_reopen_sends_new_instance(self, h) -> None:
        """§29–30: повторное открытие отправляет новый экземпляр, прежний — архив."""
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
        self.assertEqual(
            h.posts(),
            [ANNOUNCEMENT_TEXT, ANNOUNCEMENT_TEXT],
            "новый полноценный экземпляр отправлен, прежний остался в чате",
        )
        self.assertNotIn(
            first.message_ids[0],
            h.deleted_ids(),
            "архивный экземпляр не удаляется",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
