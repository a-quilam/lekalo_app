# tests/test_stage252_acceptance_tester.py
"""
Poster 2.5.2 — независимая приёмка Tester'а (21 сценарий §9).

Проверяется ПОЛЬЗОВАТЕЛЬСКИЙ контракт этапа, а не реализация Developer:
структура `reply_markup`, `message_id` и порядок сообщений, фактические
вызовы `edit*`/`delete*`, результат рендера. Всё — через настоящее
`Application.process_update` (FakeBot + временная SQLite), реальные
`post_bot.db`, `.env` и сеть Telegram не используются.

Блоки:

А. Одна публикация = одно место управления (§2, сц. 1–7).
Б. Дополнительные поля (§4, сц. 8–10).
В. Кирпичики «Назад» (§3.1, сц. 11).
Г. Редактор шаблона (§5, сц. 12–14).
Д. Условные части (§6–7, сц. 15–20).
Итог: замороженные снимки анонса (§8, сц. 21).

Отдельный помощник `markup_of` моделирует семантику НАСТОЯЩЕГО Telegram:
`editMessageText`/`editMessageCaption` БЕЗ параметра `reply_markup`
сохраняет прежнюю inline-клавиатуру, а снятие — только явная передача
`reply_markup=None` или `editMessageReplyMarkup(None)`. FakeBot этого
различия не делает, поэтому проверки «управление осталось у анонса»
опираются на честную модель, а не на запись последнего вызова.

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \\
        -m unittest tests.test_stage252_acceptance_tester -v
"""

from __future__ import annotations

import asyncio
import re
import unittest

from sqlalchemy import select, text

from tests import ui_texts_expect as cat

try:
    import announcement_controls as controls
    from announcement_instances import get_instance
    from announcement_store import (
        add_announcement_extra,
        build_announcement_preview,
        choose_announcement_template,
        confirm_announcement_place,
        get_announcement_draft,
        save_announcement_value,
        start_announcement_wizard,
    )
    from brick_store import get_system_brick
    from conditional_parts import (
        SECTION_CONTENT,
        SECTION_EXTENDED_CONTENT,
        create_conditional_part,
        delete_conditional_part,
        list_conditional_parts,
    )
    from handlers.announcement_wizard import AW_PREVIEW, CONVERSATION_NAME
    from models import PlaceAdmin, Template, utcnow
    from place_store import create_place
    from template_store import (
        create_personal_template,
        get_standard_template,
        update_template_content,
    )
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        inline_buttons,
        is_panel_record,
        scenario_test,
    )
    from tests.test_stage25_conditional_ui import (
        _open_card,
        _open_rules,
        _parts,
        _template_with_system_time_bricks,
        _user,
    )
    from tests.test_stage25_delivery_instances import (
        _delivered_with_photos,
        _delivered_without_photos,
        _only_active,
    )
    from tests.test_stage27b_announcement_ui import (
        _choose_personal,
        _latest_draft,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )
    from tests.test_stage252_template_editor import ent, span

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


EDIT_TITLE = "✏️ Изменить анонс"
MAIN_MENU_TITLE = "Здравствуйте! 👋"
STALE_TEXT = controls.STALE_INSTANCE_TEXT
EXTRAS_LABEL = "🧩 Дополнительные поля"
PANEL_TEXT = "🛠 Управление анонсом"
TemplateSpec = tuple[str, str, str, dict[str, object]]
PLAIN_SPEC: TemplateSpec = ("main", "Основное поле", "short_text", {})
EXTRA_SPEC: TemplateSpec = ("extra_x", "Доп. поле", "short_text", {})


def _catalog_block_texts(block) -> list[str]:
    """Все строки блока Rich-каталога (`utils.rich_content.Block`)."""
    from utils.rich_content import (
        BoldPart,
        DetailsBlock,
        LinkPart,
        ListBlock,
        ParagraphBlock,
        QuoteBlock,
        SlideshowBlock,
        TableBlock,
    )

    def parts(items) -> list[str]:
        values: list[str] = []
        for part in items:
            if isinstance(part, str):
                values.append(part)
            elif isinstance(part, (LinkPart, BoldPart)):
                values.append(part.text)
        return values

    if isinstance(block, ParagraphBlock):
        return parts(block.parts)
    if isinstance(block, ListBlock):
        return [text for item in block.items for text in parts(item)]
    if isinstance(block, TableBlock):
        return list(block.header) + [cell for row in block.rows for cell in row]
    if isinstance(block, QuoteBlock):
        return [block.text] + ([block.credit] if block.credit else [])
    if isinstance(block, DetailsBlock):
        return [block.summary] + [
            text for child in block.blocks for text in _catalog_block_texts(child)
        ]
    if isinstance(block, SlideshowBlock):
        return [slide.id for slide in block.slides]
    return []


# --- честная модель чата ----------------------------------------------------


def _call_ids(h, call: dict) -> tuple[int, ...]:
    """message_id, которых касается запись вызова."""
    if call["type"] in ("edit_message", "edit_markup", "edit_caption", "edit_media"):
        message_id = call.get("message_id")
        return (int(message_id),) if message_id is not None else ()
    return tuple(int(mid) for mid in h.bot.call_message_ids.get(id(call), ()))


def markup_of(h, message_id: int):
    """
    Итоговая inline-клавиатура сообщения с семантикой Telegram.

    `editMessageText`/`editMessageCaption` без `reply_markup` клавиатуру НЕ
    снимают (в Bot API её надо передавать явно), поэтому такие вызовы
    клавиатуру сохраняют. `editMessageReplyMarkup(None)` — снимает, а
    удалённое сообщение клавиатуры не несёт вовсе.
    """
    if int(message_id) in {int(mid) for mid in h.deleted_ids()}:
        return None
    markup = None
    for call in h.calls:
        if int(message_id) not in _call_ids(h, call):
            continue
        kind = call["type"]
        if kind in ("message", "photo", "media_group"):
            markup = call.get("reply_markup")
        elif kind == "edit_markup":
            markup = call.get("reply_markup")
        elif kind in ("edit_message", "edit_caption", "edit_media"):
            if "reply_markup" in call:
                markup = call.get("reply_markup")
    return markup


def buttons_of(h, message_id: int) -> list[tuple[str, str | None]]:
    return inline_buttons(markup_of(h, message_id))


def callbacks_of(h, message_id: int) -> list[str]:
    return [data or "" for _label, data in buttons_of(h, message_id)]


def live_anp_messages(h) -> dict[int, list[str]]:
    """Живые (не удалённые) сообщения чата с `anp_`-кнопками и их callback'и."""
    deleted = {int(mid) for mid in h.deleted_ids()}
    out: dict[int, list[str]] = {}
    for call in h.calls:
        for message_id in _call_ids(h, call):
            if message_id in deleted:
                continue
            data = [
                value
                for value in callbacks_of(h, message_id)
                if value.startswith("anp_")
            ]
            if data:
                out[message_id] = data
    return out


def panel_records(h) -> list[dict]:
    return [call for call in h.calls if is_panel_record(call)]


def panel_sends(h) -> list[dict]:
    """
    Вызовы, СОЗДАВШИЕ панель управления (§22).

    2.5.3 §5: список текстовых полей правит текст панели (`editMessageText`),
    поэтому `panel_records` видит и правки. Проверка «панель не
    продублирована» считает именно отправки: правка — ожидаемая работа.
    """
    return [call for call in panel_records(h) if call["type"] == "message"]


def panel_id(h) -> int | None:
    records = panel_records(h)
    if not records:
        return None
    ids = h.bot.call_message_ids.get(id(records[-1]), ())
    return int(ids[0]) if ids else None


def control_data(h, action: str) -> str:
    """`callback_data` кнопки управления активным анонсом; ошибка, если нет."""
    found = h.control_callback(action)
    if found is None:
        raise AssertionError(f"нет кнопки {action}: {live_anp_messages(h)}")
    return found


def screen_callbacks(h, prefix: str) -> list[str]:
    return [
        data or ""
        for _label, data in h.screen_buttons()
        if (data or "").startswith(prefix)
    ]


def screen_labels(h) -> list[str]:
    return [label for label, _data in h.screen_buttons()]


def screen_data(h, prefix: str) -> str:
    found = screen_callbacks(h, prefix)
    if not found:
        raise AssertionError(f"нет callback {prefix}: {h.screen_buttons()}")
    return found[0]


def _field_callback(h) -> str:
    """`callback_data` первого поля списка «📝 Текстовые поля» (2.5.3 §5)."""
    fields = h.live_callbacks("anf_sel:")
    if not fields:
        raise AssertionError(f"нет списка полей: {h.live_buttons()}")
    return fields[0]


def main_menu_shown(h) -> bool:
    return h.screen_text().startswith(MAIN_MENU_TITLE)


def texts_in_chat(h, needle: str) -> list[int]:
    """message_id живых сообщений, содержащих подстроку (текст или подпись)."""
    deleted = {int(mid) for mid in h.deleted_ids()}
    out: list[int] = []
    for (_chat_id, message_id), (kind, body, _markup) in h.bot._message_content.items():
        if int(message_id) in deleted or kind == "rich":
            continue
        if body and needle in body:
            out.append(int(message_id))
    return sorted(out)


# --- доставка с расширенной версией ----------------------------------------


async def _deliver(
    h,
    *,
    specs,
    content: str,
    extended: str | None = None,
    photos: int = 0,
    main_value: str = "Значение",
):
    """Личный шаблон → значения → шаг фото → доставка. Возвращает draft."""
    await h.text("/start")
    place_id, _alias = _seed_place(h)
    template_id, _revision, _bricks = _seed_personal_template(
        h,
        specs=specs,
        content=content,
        extended_content=extended,
    )
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    for _attempt in range(6):
        if cat.text("wizard.photos.title") in h.screen_text():
            break
        await h.text(main_value)
    if photos == 0:
        await h.callback(screen_data(h, "aw_photo_none"))
    elif photos == 1:
        await h.photo("photo-0")
    else:
        for index in range(photos):
            await h.photo(f"photo-{index}", media_group_id="album")
        await asyncio.sleep(0.4)
    draft = _latest_draft(h)
    if h.draft_status(draft.id) != "sent":
        raise AssertionError(f"не доставлено: {h.screen_text()[:120]}")
    return draft


def _stored(h, draft_id: int):
    """Снимок черновика из БД (обёртка над помощником соседнего набора)."""
    from tests.test_stage25_delivery_instances import _stored as stored_helper

    return stored_helper(h, draft_id)


def _instance(h, instance_id: int):
    with h.session_factory() as session:
        return get_instance(session, instance_id)


def _details(h, draft_id: int):
    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
        )


# ===========================================================================
# Блок А. Одна публикация = одно место управления (§2, сц. 1–7)
# ===========================================================================


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class SingleControlTests(unittest.TestCase):
    @scenario_test
    async def test_1_zero_photos_one_message_with_control(self, h) -> None:
        """Сц. 1: ровно одно сообщение анонса с текстом и клавиатурой."""
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        stored = _stored(h, draft.id)
        content_id = stored.delivered_message_ids[0]

        # Одна копия текста анонса в чате, и это сообщение с управлением.
        copies = texts_in_chat(h, stored.publication_text)
        self.assertEqual(copies, [content_id], "вторая копия текста анонса")
        callbacks = callbacks_of(h, content_id)
        self.assertTrue(callbacks, "у анонса нет клавиатуры управления")
        self.assertTrue(all(item.startswith("anp_") for item in callbacks), callbacks)
        # Единственное место управления и никакой отдельной панели.
        self.assertEqual(list(live_anp_messages(h)), [content_id])
        self.assertEqual(panel_records(h), [])
        self.assertNotIn(EDIT_TITLE, h.screen_text())

    @scenario_test
    async def test_2_one_photo_one_send_photo_with_control(self, h) -> None:
        """Сц. 2: один sendPhoto с подписью и управлением, дубля нет."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 1)
        stored = _stored(h, draft.id)
        content_id = stored.delivered_message_ids[0]

        photos = [call for call in h.calls if call["type"] == "photo"]
        self.assertEqual(len(photos), 1, "фото отправлено больше одного раза")
        self.assertEqual(photos[0]["caption"], stored.publication_text)
        self.assertEqual(
            texts_in_chat(h, stored.publication_text),
            [content_id],
            "второй экран с тем же текстом",
        )
        self.assertTrue(
            all(item.startswith("anp_") for item in callbacks_of(h, content_id))
        )
        self.assertEqual(list(live_anp_messages(h)), [content_id])
        self.assertEqual(panel_records(h), [])
        self.assertNotIn(EDIT_TITLE, h.screen_text())

    @scenario_test
    async def test_3_album_one_media_group_and_exactly_one_panel(self, h) -> None:
        """Сц. 3: одна медиагруппа и ровно одна панель `panel_keyboard`."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 3)
        stored = _stored(h, draft.id)

        groups = [call for call in h.calls if call["type"] == "media_group"]
        self.assertEqual(len(groups), 1, "медиагруппа отправлена не один раз")
        records = panel_records(h)
        self.assertEqual(len(records), 1, f"панелей: {len(records)}")
        self.assertIn(PANEL_TEXT, records[0].get("text") or "")
        panel = panel_id(h)
        self.assertIsNotNone(panel)
        self.assertEqual(
            list(live_anp_messages(h)), [panel], "управление не только на панели"
        )
        for message_id in stored.delivered_message_ids:
            self.assertIsNone(markup_of(h, message_id), "кнопки на вложении альбома")
        self.assertNotIn(EDIT_TITLE, h.screen_text())

    @scenario_test
    async def test_4_successful_edit_persist_inplace_close_control_kept(
        self, h
    ) -> None:
        """Сц. 4: сохранение → БД, правка на месте, вопрос закрыт, управление живо."""
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        active = _only_active(h, draft.id)
        message_id = active.message_ids[0]

        # 2.5.3 §5 + 2.5.4 §1: «📝 Текстовые поля» — список на самом анонсе;
        # отдельного экрана нет, выбор поля переключает клавиатуру анонса в
        # режим ввода значения (служебного вопроса тоже нет).
        await h.callback(control_data(h, controls.ACTION_DATA))
        self.assertIsNone(h.screen_message_id(), "служебного экрана нет")
        field_data = _field_callback(h)
        await h.callback(field_data)
        self.assertIsNone(h.screen_message_id(), "служебный экран ввода")
        self.assertTrue(h.live_callbacks("anq_"), "клавиатура ввода не открылась")
        await h.text("Обновлено")

        self.assertEqual(
            _details(h, draft.id).publication_text, "Обновлено", "правка не в БД"
        )
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id), "Обновлено", "правка не на месте"
        )
        self.assertEqual(h.posts(), ["Обновлено"], "создана вторая версия анонса")
        # 2.5.4 §1: после сохранения — снова список полей на тех же кнопках.
        self.assertFalse(h.live_callbacks("anq_"), "ввод не закрыт")
        self.assertTrue(h.live_callbacks("anf_sel:"), "список полей не вернулся")
        # «◀️ Назад» возвращает управление самому анонсу.
        await h.callback(h.live_callbacks("anf_back:")[0])
        callbacks = callbacks_of(h, message_id)
        self.assertTrue(
            callbacks and all(item.startswith("anp_") for item in callbacks),
            f"управление потеряно: {callbacks}, edits={[c['type'] for c in h.calls[-4:]]}",
        )
        self.assertEqual(list(live_anp_messages(h)), [message_id])
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PREVIEW)

    @scenario_test
    async def test_4b_album_edit_updates_caption_and_keeps_panel(self, h) -> None:
        """Сц. 4 для альбома: подпись на месте, панель не дублируется."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 2)
        active = _only_active(h, draft.id)
        panel = active.panel_message_id
        caption_id = active.message_ids[0]

        # 2.5.3 §5 + 2.5.4 §1: у медиагруппы список полей — правка текста
        # панели; выбор поля переключает ту же панель в режим ввода.
        await h.callback(control_data(h, controls.ACTION_DATA))
        await h.callback(_field_callback(h))
        self.assertTrue(h.live_callbacks("anq_"), "клавиатура ввода не открылась")
        await h.text("Обновлено")

        self.assertEqual(h.bot.content_text(h.chat_id, caption_id), "Обновлено")
        self.assertEqual(
            len([call for call in h.calls if call["type"] == "media_group"]),
            1,
            "альбом переиздан",
        )
        self.assertEqual(len(panel_sends(h)), 1, "панель продублирована")
        # 2.5.4 §1: после сохранения панель снова показывает список полей,
        # «◀️ Назад» возвращает управление анонсу (`anp_`).
        self.assertTrue(h.live_callbacks("anf_sel:"), "список полей не вернулся")
        await h.callback(h.live_callbacks("anf_back:")[0])
        self.assertTrue(
            all(item.startswith("anp_") for item in callbacks_of(h, int(panel)))
        )

    @scenario_test
    async def test_5_failed_input_keeps_temporary_screen(self, h) -> None:
        """Сц. 5: неверный ввод не теряет клавиатуру и не меняет анонс."""
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _rev, _bricks = _seed_personal_template(
            h,
            specs=[("main", "Основное поле", "number", {})],
            content="[main]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("42")
        await h.callback(screen_data(h, "aw_photo_none"))
        draft = _latest_draft(h)
        active = _only_active(h, draft.id)
        message_id = active.message_ids[0]

        await h.callback(control_data(h, controls.ACTION_DATA))
        await h.callback(_field_callback(h))
        self.assertTrue(h.live_callbacks("anq_"), "клавиатура ввода не открылась")
        await h.text("не число")

        # 2.5.4 §1: экрана-вопроса больше нет, поэтому проверяется сохранность
        # самого анонса и клавиатуры ввода; ошибка объясняется ответом.
        self.assertIn("число", "\n".join(h.reply_texts()).lower(), h.reply_texts()[:3])
        self.assertEqual(h.bot.content_text(h.chat_id, message_id), "42")
        self.assertTrue(h.live_callbacks("anq_"), "клавиатура ввода потеряна")
        self.assertIsNone(h.screen(), "служебный экран не создаётся")

    @scenario_test
    async def test_6_menu_strips_keyboard_keeps_message_and_clears_dialog(
        self, h
    ) -> None:
        """Сц. 6 (панель): клавиатура снята, сообщение живо, диалог очищен."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 2)
        active = _only_active(h, draft.id)
        message_id = active.message_ids[0]
        panel = int(active.panel_message_id)
        posts_before = h.posts()

        await h.callback(control_data(h, controls.ACTION_MENU))

        archived = _instance(h, active.id)
        self.assertIsNotNone(archived)
        self.assertTrue(archived.is_archived, "экземпляр не архивирован")
        self.assertIsNone(markup_of(h, panel), "панель не закрыта")
        self.assertIn(panel, h.deleted_ids(), "панель не удалена")
        self.assertNotIn(
            message_id, h.deleted_ids(), "сообщение анонса удалено — нельзя"
        )
        self.assertTrue(main_menu_shown(h), h.screen_text()[:80])
        self.assertIsNone(
            h.dialog_state(name=CONVERSATION_NAME), "состояние диалога не очищено"
        )
        self.assertEqual(h.posts(), posts_before, "анонс переиздан")

    @scenario_test
    async def test_6b_menu_from_editor_strips_keyboard(self, h) -> None:
        """Сц. 6 (из списка полей): управление снято с фактического сообщения."""
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        active = _only_active(h, draft.id)
        message_id = active.message_ids[0]

        # 2.5.3 §5: вместо экрана «✏️ Изменить анонс» — список текстовых
        # полей на управлении анонса. Меню доступно после «◀️ Назад».
        await h.callback(control_data(h, controls.ACTION_DATA))
        self.assertIsNone(h.screen_message_id(), "служебного экрана нет")
        self.assertTrue(h.live_callbacks("anf_back:"), "список полей не открыт")
        await h.callback(h.live_callbacks("anf_back:")[0])
        await h.callback("main_menu")

        self.assertIsNone(markup_of(h, message_id), "клавиатура анонса осталась")
        self.assertNotIn(message_id, h.deleted_ids())
        self.assertTrue(main_menu_shown(h))
        self.assertTrue(_instance(h, active.id).is_archived)
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))

    @scenario_test
    async def test_7_reopen_new_instance_and_stale_callbacks_are_noop(self, h) -> None:
        """Сц. 7: новый управляемый инстанс; старые кнопки — безопасный no-op."""
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        first = _only_active(h, draft.id)
        old_message = first.message_ids[0]
        old_data = control_data(h, controls.ACTION_DATA)
        posts_before = h.posts()
        details_before = _details(h, draft.id).publication_text

        await h.callback("main_menu")
        await h.callback("my_announcements")
        await h.callback(screen_data(h, "aw_open"))

        active = _only_active(h, draft.id)
        self.assertNotEqual(active.id, first.id, "открыт прежний инстанс")
        self.assertEqual(active.generation, first.generation + 1)
        self.assertNotEqual(active.message_ids, first.message_ids)
        self.assertEqual(_instance(h, first.id).status, "archived")
        self.assertIsNone(markup_of(h, old_message), "у прежнего снята клавиатура")
        self.assertTrue(
            all(
                item.startswith("anp_")
                for item in callbacks_of(h, active.message_ids[0])
            )
        )
        self.assertEqual(len(h.posts()), len(posts_before) + 1)

        # Устаревшие кнопки прежнего инстанса: только alert, ничего не меняется.
        calls_before = len(h.calls)
        answers_before = len(h.answers())
        await h.callback(old_data)
        changes = [
            call for call in h.calls[calls_before:] if call["type"] != "callback_answer"
        ]
        self.assertEqual(changes, [], f"старая кнопка что-то сделала: {changes}")
        answers = h.answers()[answers_before:]
        self.assertEqual(len(answers), 1)
        self.assertTrue(answers[0].get("show_alert"))
        self.assertEqual(answers[0].get("text"), STALE_TEXT)
        # Данные анонса не изменились.
        self.assertEqual(_details(h, draft.id).publication_text, details_before)
        self.assertEqual(_instance(h, first.id).status, "archived")


# ===========================================================================
# Блок Б. Дополнительные поля (§4, сц. 8–10)
# ===========================================================================


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ExtrasVisibilityTests(unittest.TestCase):
    @scenario_test
    async def test_8_simple_template_has_no_extras_button_anywhere(self, h) -> None:
        """Сц. 8: шаблон без расширенной версии — кнопки нет ни в карточке, ни в управлении."""
        await h.text("/start")
        draft = await _deliver(h, specs=[PLAIN_SPEC], content="[main]")
        stored = _stored(h, draft.id)
        content_id = stored.delivered_message_ids[0]
        self.assertNotIn(
            EXTRAS_LABEL, [label for label, _d in buttons_of(h, content_id)]
        )

        # В карточке шаблона кнопки доп. полей тоже нет.
        with h.session_factory() as session:
            template = create_personal_template(
                session,
                actor_telegram_user_id=h.user_id,
                name="Простой",
                content="[время_начала]",
            )
            session.commit()
            template_id = template.template_id
        await _open_card(h, template_id)
        self.assertNotIn(EXTRAS_LABEL, screen_labels(h))
        self.assertNotIn(
            f"tp_extras:{template_id}", " ".join(screen_callbacks(h, "tp_"))
        )

    @scenario_test
    async def test_9_extended_without_new_bricks_has_no_extras_button(self, h) -> None:
        """Сц. 9: расширенная версия без доп. кирпичиков — кнопки нет.

        Прямой callback скрытой кнопки не открывает «пустой экран добавления»:
        он показывает честное «полей нет» без кнопок добавления.
        """
        await h.text("/start")
        draft = await _deliver(
            h,
            specs=[PLAIN_SPEC],
            content="[main]",
            extended="[main] (расширенно)",
        )
        stored = _stored(h, draft.id)
        content_id = stored.delivered_message_ids[0]
        self.assertNotIn(
            EXTRAS_LABEL,
            [label for label, _d in buttons_of(h, content_id)],
            "кнопка доп. полей при пустом списке",
        )

        instance = _only_active(h, draft.id)
        await h.callback(f"anp_extras:{instance.id}")
        labels = screen_labels(h)
        self.assertNotIn(EXTRAS_LABEL, labels)
        self.assertFalse(
            screen_callbacks(h, "aw_exedit:"), "пустой экран добавления открыт"
        )
        self.assertTrue(h.screen_text().strip(), "экран без объяснения")

    @scenario_test
    async def test_10_extended_with_extra_bricks_button_and_manage(self, h) -> None:
        """Сц. 10: кнопка есть; выбранные extras управляются (удаление)."""
        await h.text("/start")
        draft = await _deliver(
            h,
            specs=[PLAIN_SPEC, EXTRA_SPEC],
            content="[main]",
            extended="[main]\n[extra_x]",
        )
        stored = _stored(h, draft.id)
        content_id = stored.delivered_message_ids[0]
        labels = [label for label, _d in buttons_of(h, content_id)]
        self.assertIn(EXTRAS_LABEL, labels, f"кнопки нет: {labels}")

        instance = _only_active(h, draft.id)
        await h.callback(f"anp_extras:{instance.id}")
        await h.callback(screen_data(h, "aw_exedit:"))
        await h.text("Дополнительное значение")
        extras = _details(h, draft.id).extras
        self.assertEqual(len(extras), 1, f"выбранное поле не сохранено: {extras}")
        delete_data = screen_data(h, "aw_exdel:")
        await h.callback(delete_data)
        self.assertEqual(list(_details(h, draft.id).extras), [], "поле не удалено")
        self.assertIn("удалено", h.screen_text().lower(), h.screen_text()[:120])


# ===========================================================================
# Блок В. Кирпичики «Назад» (§3.1, сц. 11)
# ===========================================================================


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class BricksBackTests(unittest.TestCase):
    @scenario_test
    async def test_11_back_from_manage_returns_to_manage(self, h) -> None:
        """Сц. 11: «Управление → Кирпичики» → «Назад» → «Управление»."""
        await h.text("/start")
        await h.callback("manage")
        await h.callback("manage_bricks")
        self.assertIn("Мои кирпичики", h.screen_text())
        self.assertIn(("◀️ Назад", "bk_back"), h.screen_buttons())

        await h.callback("bk_back")
        self.assertIn("⚙️ Управление", h.screen_text())
        self.assertIn(("🧩 Кирпичики", "manage_bricks"), h.screen_buttons())
        self.assertNotIn("Мои кирпичики", h.screen_text())

    @scenario_test
    async def test_11b_back_survives_restart_and_fallback_is_safe(self, h) -> None:
        """Сц. 11: навигация переживает рестарт; сессия без origin — фолбэк."""
        await h.text("/start")
        await h.callback("manage")
        await h.callback("manage_bricks")
        await h.restart()
        await h.callback("bk_back")
        self.assertIn("⚙️ Управление", h.screen_text())

        # Фолбэк: прямой вход /bricks не знает источника — «Управление».
        await h.text("/bricks")
        self.assertIn("Мои кирпичики", h.screen_text())
        await h.callback("bk_back")
        self.assertIn("⚙️ Управление", h.screen_text())
        cat.assert_not_shows(self, h.screen_text(), "errors.internal")

    @scenario_test
    async def test_11c_back_from_wizard_bricks_returns_to_wizard(self, h) -> None:
        """Сц. 11: из шага конструктора возврат в конструктор, не в меню."""
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        self.assertRegex(h.screen_text(), cat.question_re(1, 1))

        # Старый вход в кирпичики с шага конструктора: источник — мастер.
        await h.callback("bricks")
        self.assertIn("Мои кирпичики", h.screen_text())
        await h.restart()
        await h.callback("bk_back")
        self.assertRegex(h.screen_text(), cat.question_re(1, 1), "не вернулись на шаг")
        self.assertNotIn("⚙️ Управление", h.screen_text())


# ===========================================================================
# Блок Г. Редактор шаблона (§5, сц. 12–14)
# ===========================================================================


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TemplateCardTests(unittest.TestCase):
    @scenario_test
    async def test_12_card_buttons_exact_and_no_preview(self, h) -> None:
        """Сц. 12: две кнопки правки; «Текст»/«Расширенный»/предпросмотра нет."""
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            specs=[PLAIN_SPEC, EXTRA_SPEC],
            content="[main]",
            extended_content="[main]\n[extra_x]",
        )
        await _open_card(h, template_id)
        labels = screen_labels(h)
        # 2.6.1 §6/§7: терминология «Расширенный шаблон» едина во всех меню —
        # на карточке две кнопки правки: простая и расширенная.
        self.assertIn(cat.text("buttons.templates.tekst"), labels, labels)
        self.assertIn(
            cat.text("buttons.templates.rasshirennyy_shablon"), labels, labels
        )
        self.assertNotIn("✏️ Текст", labels, "старая подпись кнопки")
        self.assertNotIn("✏️ Расширенный", labels, "старая подпись кнопки")
        self.assertFalse(
            [label for label in labels if "Предпросмотр" in label or "👁" in label],
            f"кнопка предпросмотра осталась: {labels}",
        )
        self.assertEqual(screen_callbacks(h, "tp_preview"), [], "route предпросмотра")
        self.assertIn(f"tp_edit:{template_id}", screen_callbacks(h, "tp_edit:"))
        self.assertIn(f"tp_edit_ext:{template_id}", screen_callbacks(h, "tp_edit_ext:"))

    @scenario_test
    async def test_13_first_extended_message_with_emoji_bold_link_accepted(
        self, h
    ) -> None:
        """Сц. 13: первое сообщение (эмодзи+bold+ссылка) принято сразу."""
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            specs=[PLAIN_SPEC],
            content="[main]",
            extended_content="[main]",
        )
        await h.text("/templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_edit_ext:{template_id}")

        body = "🔥 Итог [main] Сайт"
        before = len(h.screen_texts())
        await h.text(
            body,
            entities=[
                ent("bold", *span(body, "🔥")),
                ent("text_link", *span(body, "Сайт"), url="https://e.example/x"),
            ],
        )
        text = h.screen_text()
        cat.assert_not_shows(self, text, "errors.internal")
        self.assertNotIn("Отправьте", text.split("\n")[0], "повторный запрос ввода")
        self.assertIn("tp_save", [data for _l, data in h.screen_buttons()])
        self.assertEqual(len(h.screen_texts()), before + 1, "экран перерисован дважды")
        self.assertIn("**🔥**", text)
        self.assertIn("(https://e.example/x)", text)

        await h.callback("tp_save")
        from tests.test_poster2_241_markup_acceptance import _version_extended

        saved = _version_extended(h, template_id, 2)
        assert saved is not None
        self.assertIn("**🔥**", saved)
        self.assertIn("(https://e.example/x)", saved)
        self.assertIn("[main]", saved)

    @scenario_test
    async def test_14_formatting_links_bricks_and_independence(self, h) -> None:
        """Сц. 14: форматирование/ссылки/{{...}} сохранены; версии независимы."""
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            specs=[PLAIN_SPEC, EXTRA_SPEC],
            content="[main]",
            extended_content="[main]\n[extra_x]",
        )
        await h.text("/templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_edit_ext:{template_id}")
        body = "🎉 [main] [extra_x] {{текст}} код"
        await h.text(
            body,
            entities=[
                ent("bold", *span(body, "🎉")),
                ent("italic", *span(body, "код")),
            ],
        )
        await h.callback("tp_save")
        from tests.test_poster2_241_markup_acceptance import (
            _version_content,
            _version_extended,
        )

        extended = _version_extended(h, template_id, 2)
        assert extended is not None
        simple = _version_content(h, template_id, 2)
        self.assertIn("**🎉**", extended)
        self.assertIn("*код*", extended)
        self.assertIn(r"\{\{текст\}\}", extended)
        self.assertIn("[main]", extended)
        self.assertIn("[extra_x]", extended)
        self.assertEqual(simple, "[main]", "простая версия изменена расширенной")


# ===========================================================================
# Блок Д. Условные части (§6–7, сц. 15–20)
# ===========================================================================


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ConditionalEntryTests(unittest.TestCase):
    @scenario_test
    async def test_15_conditional_entry_only_for_extended(self, h) -> None:
        """Сц. 15: кнопка только у расширенного; у простого её нет, callback отказан."""
        with h.session_factory() as session:
            user = _user(session, h.user_id)
            simple = create_personal_template(
                session,
                actor_telegram_user_id=user.telegram_user_id,
                name="Простой",
                content="[время_начала]",
            )
            session.commit()
            simple_id = simple.template_id
        await _open_card(h, simple_id)
        self.assertFalse(screen_callbacks(h, f"tp_cond:{simple_id}"))
        answers_before = len(h.answer_texts())
        await h.callback(f"tp_cond:{simple_id}")
        self.assertGreater(len(h.answer_texts()), answers_before, "отказа не было")
        self.assertNotIn("Выберите кирпичик", h.screen_text())
        self.assertEqual(_parts(h, simple_id), [])

        extended_id, _revision = _template_with_system_time_bricks(h)
        # Отказ не закрывает карточку простого шаблона: `tp_open` другого
        # шаблона принимается только из списка, поэтому сначала «К списку».
        await h.callback("tp_hub")
        await h.callback(f"tp_open:{extended_id}")
        self.assertIn(
            (
                cat.text("buttons.templates.uslovnye_chasti"),
                f"tp_cond:{extended_id}",
            ),
            h.screen_buttons(),
        )

    @scenario_test
    async def test_15b_no_origin_check_for_standard_template(self, h) -> None:
        """Сц. 15: origin-проверок нет — стандарт места открывает правила."""
        with h.session_factory() as session:
            user = _user(session, h.user_id)
            place = create_place(
                session,
                "Дом культуры",
                city="Тестовый город",
                created_by_user_id=user.id,
            )
            session.add(
                PlaceAdmin(
                    place_id=place.id,
                    user_id=user.id,
                    assigned_by_user_id=None,
                    assigned_at=utcnow(),
                )
            )
            session.commit()
            place_id = place.id
            template = session.scalar(
                select(Template).where(
                    Template.place_id == place_id, Template.kind == "standard"
                )
            )
            assert template is not None
            template_id = template.id
            details = get_standard_template(session, h.user_id, place_id)
            updated = update_template_content(
                session,
                actor_telegram_user_id=h.user_id,
                template_id=template_id,
                expected_revision=details.revision,
                content=details.current_version.content,
                extended_content=details.current_version.content,
            )
            self.assertTrue(updated.ok, updated.message)

        await h.callback(f"tp_std:{place_id}")
        cat.assert_shows(self, h.screen_text(), "templates.card.title")
        await h.callback(f"tp_cond:{template_id}")
        self.assertIn(
            cat.text("templates.cond.title"),
            h.screen_text(),
            "ложный отказ по origin",
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ConditionalEngineTests(unittest.TestCase):
    def _preview(self, h, template_id: int, values: dict[int, str]) -> str:
        """Рендер готового анонса через доменный путь (снимок + значения)."""
        with h.session_factory() as session:
            draft = start_announcement_wizard(
                session, actor_telegram_user_id=h.user_id, place_id=self.place_id
            )
            confirmed = confirm_announcement_place(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=draft.draft_id,
                expected_revision=draft.revision,
            )
            details = choose_announcement_template(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=confirmed.draft_id,
                template_id=template_id,
                expected_revision=confirmed.revision,
            )
        for brick_id, value in values.items():
            with h.session_factory() as session:
                details = save_announcement_value(
                    session,
                    actor_telegram_user_id=h.user_id,
                    draft_id=details.draft_id,
                    brick_id=brick_id,
                    value=value,
                    expected_revision=details.revision,
                )
        with h.session_factory() as session:
            return build_announcement_preview(
                session, actor_telegram_user_id=h.user_id, draft_id=details.draft_id
            ).html

    def _preview_extended(
        self, h, template_id: int, values: dict[int, str], extra_id: int
    ) -> str:
        """Готовый анонс по расширенному тексту (доп. поле включает его)."""
        with h.session_factory() as session:
            draft = start_announcement_wizard(
                session, actor_telegram_user_id=h.user_id, place_id=self.place_id
            )
            confirmed = confirm_announcement_place(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=draft.draft_id,
                expected_revision=draft.revision,
            )
            details = choose_announcement_template(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=confirmed.draft_id,
                template_id=template_id,
                expected_revision=confirmed.revision,
            )
        for brick_id, value in values.items():
            with h.session_factory() as session:
                if brick_id == extra_id:
                    details = add_announcement_extra(
                        session,
                        actor_telegram_user_id=h.user_id,
                        draft_id=details.draft_id,
                        brick_id=brick_id,
                        value=value,
                        expected_revision=details.revision,
                    )
                    continue
                details = save_announcement_value(
                    session,
                    actor_telegram_user_id=h.user_id,
                    draft_id=details.draft_id,
                    brick_id=brick_id,
                    value=value,
                    expected_revision=details.revision,
                )
        with h.session_factory() as session:
            return build_announcement_preview(
                session, actor_telegram_user_id=h.user_id, draft_id=details.draft_id
            ).html

    async def _setup(
        self,
        h,
        content: str,
        extended: str | None = None,
        *,
        specs: list[TemplateSpec] | None = None,
    ):
        """Сев места и шаблона (первый /start создаёт пользователя)."""
        await h.text("/start")
        self.place_id, _alias = _seed_place(h)
        template_id, revision, bricks = _seed_personal_template(
            h, specs=specs or [PLAIN_SPEC], content=content, extended_content=extended
        )
        return template_id, revision, bricks

    @scenario_test
    async def test_16_repeated_brick_one_rule_and_literal_text_untouched(
        self, h
    ) -> None:
        """Сц. 16: повтор = одно правило; литеральный текст не автоочищается."""
        # Строка содержит кирпичик `[cost]`, поэтому она остаётся видимой при
        # пустом `[main]`: иначе компилятор скрыл бы её целиком (H2) и тире
        # исчезло бы не из-за правила, а из-за скрытия brick-строки.
        template_id, revision, bricks = await self._setup(
            h,
            content="[main] — [main], [cost]\nСтатический текст",
            extended="[main] — [main], [cost]\nСтатический текст",
            specs=[
                PLAIN_SPEC,
                ("cost", "Цена", "short_text", {}),
            ],
        )
        brick_id = bricks["main"]
        with h.session_factory() as session:
            created = create_conditional_part(
                session,
                actor_telegram_user_id=h.user_id,
                template_id=template_id,
                expected_revision=revision,
                section=SECTION_EXTENDED_CONTENT,
                brick_id=brick_id,
                fragment=" — [main]",
                occurrence=1,
            )
            self.assertTrue(created.ok, created.message)
        await _open_rules(h, template_id)
        # §3: один кирпичик — одна кнопка и одно условие, сколько бы раз он
        # ни встречался в тексте.
        self.assertEqual(
            screen_labels(h).count("Основное поле ✓"), 1, h.screen_buttons()
        )

        # Без правила пунктуация вокруг пустого поля НЕ вычищается.
        with h.session_factory() as session:
            rules = list_conditional_parts(session, h.user_id, template_id)
            for rule in rules:
                delete_conditional_part(
                    session,
                    actor_telegram_user_id=h.user_id,
                    part_id=rule.id,
                    expected_revision=revision,
                )
        rendered = self._preview(
            h, template_id, {brick_id: "", bricks["cost"]: "500 ₽"}
        )
        self.assertIn("Статический текст", rendered, "литеральный текст исчез")
        self.assertIn("—", rendered, "пунктуация автоочищена")

    @scenario_test
    async def test_17_pagination_five_per_page_and_inert_indicator(self, h) -> None:
        """Сц. 17: 5/страницу, 3 страницы, средняя кнопка инертна, 1 стр — без навигации."""
        specs: list[TemplateSpec] = [
            (f"ff{index}", f"Поле {index}", "short_text", {}) for index in range(11)
        ]
        content = "\n".join(f"Строка {index}: [ff{index}]" for index in range(11))
        template_id, revision, bricks = _seed_personal_template(
            h,
            name="Много правил",
            specs=specs,
            content=content,
            extended_content=content,
        )
        with h.session_factory() as session:
            for index in range(11):
                result = create_conditional_part(
                    session,
                    actor_telegram_user_id=h.user_id,
                    template_id=template_id,
                    expected_revision=revision,
                    section=SECTION_EXTENDED_CONTENT,
                    brick_id=bricks[f"ff{index}"],
                    fragment=f"Строка {index}: [ff{index}]",
                    occurrence=1,
                )
                self.assertTrue(result.ok, result.message)
        await _open_rules(h, template_id)
        # §3: на экране только кнопки кирпичиков, без нумерованного списка.
        text = h.screen_text()
        self.assertNotIn("1. ", text, "перечня полей в теле нет")
        labels = screen_labels(h)
        for number in (0, 4):
            self.assertIn(f"Поле {number} ✓", labels)
        self.assertNotIn("Поле 5 ✓", labels, "шестой кирпичик на второй странице")
        self.assertIn("1 / 3", labels)
        self.assertNotIn("◀️", screen_labels(h), "левая стрелка на первой странице")
        self.assertEqual(screen_callbacks(h, "noop:"), ["noop:1:3"])

        before = h.screen_texts()[-1]
        calls_before = len(h.calls)
        await h.callback("noop:1:3")
        self.assertEqual(h.calls[calls_before:][0]["type"], "callback_answer")
        self.assertEqual(h.screen_texts()[-1], before, "индикатор изменил экран")

        await h.callback(screen_data(h, "tp_condp:"))
        self.assertIn("2 / 3", screen_labels(h))
        self.assertIn("◀️", screen_labels(h))
        self.assertIn("▶️", screen_labels(h))
        # На средней странице две стрелки: первая — «назад», поэтому вперёд
        # идём по второй (последней) кнопке навигации.
        await h.callback(screen_callbacks(h, "tp_condp:")[-1])
        self.assertIn("3 / 3", screen_labels(h))
        self.assertNotIn("▶️", screen_labels(h), "правая стрелка на последней странице")

        # Одна страница — навигации нет вовсе.
        one_id, _revision = _template_with_system_time_bricks(h, name="Одно правило")
        # Карточка первого шаблона ещё открыта: `tp_open` другого шаблона
        # принимается только из списка, поэтому «◀️ К списку» обязателен.
        await h.callback("tp_hub")
        await h.callback(f"tp_open:{one_id}")
        await h.callback(f"tp_cond:{one_id}")
        self.assertEqual(screen_callbacks(h, "tp_condp:"), [])
        self.assertEqual(screen_callbacks(h, "noop:"), [])

    @scenario_test
    async def test_18_three_modes_with_examples_and_applied_in_list(self, h) -> None:
        """Сц. 18: три режима с примерами; выбор применён и виден в списке."""
        template_id, _revision = _template_with_system_time_bricks(
            h, content="🕒 [время_начала]–[время_окончания]\nКонтакты"
        )
        await _open_rules(h, template_id)
        await h.callback(f"tp_condadd:{template_id}")
        with h.session_factory() as session:
            end_brick = get_system_brick(session, "время_окончания")
            assert end_brick is not None
        await h.callback(f"tp_condb:{template_id}:e:{end_brick.id}")

        text = h.screen_text()
        for expected in ("Вся строка", "С разделителем", "Свой фрагмент"):
            self.assertIn(expected, text, f"нет примера режима: {expected}")
        labels = screen_labels(h)
        for expected in (
            "Скрыть всю строку",
            "Скрыть с разделителем",
            "Скрыть свой фрагмент",
        ):
            self.assertIn(expected, labels)
        self.assertEqual(
            screen_callbacks(h, f"tp_condf:{template_id}:"),
            [
                f"tp_condf:{template_id}:line",
                f"tp_condf:{template_id}:sep",
                f"tp_condf:{template_id}:custom",
            ],
        )
        await h.callback(f"tp_condf:{template_id}:line")
        self.assertEqual(len(_parts(h, template_id)), 1)
        # §3: способ виден на экране кирпичика, а в списке — только «✓».
        self.assertIn("Время окончания ✓", screen_labels(h))
        await h.callback(f"tp_condb:{template_id}:e:{end_brick.id}")
        self.assertIn("вся строка · выбрано", h.screen_text(), "режим не виден")

    @scenario_test
    async def test_19_rule_list_is_human_readable(self, h) -> None:
        """Сц. 19: нет номеров вхождений, диапазонов и сырых фрагментов с ⏎."""
        template_id, revision, bricks = _seed_personal_template(
            h,
            specs=[
                ("main", "Основное поле", "short_text", {}),
                ("cost", "Цена", "short_text", {}),
            ],
            content="[main]\n💰 Цена: [cost]\nКонец",
            extended_content="[main]\n💰 Цена: [cost]\nКонец",
        )
        with h.session_factory() as session:
            created = create_conditional_part(
                session,
                actor_telegram_user_id=h.user_id,
                template_id=template_id,
                expected_revision=revision,
                section=SECTION_EXTENDED_CONTENT,
                brick_id=bricks["cost"],
                fragment="💰 Цена: [cost]",
                occurrence=1,
            )
            self.assertTrue(created.ok, created.message)
        await _open_rules(h, template_id)
        text = h.screen_text()
        self.assertIn("Цена ✓", screen_labels(h))
        self.assertNotIn("💰 Цена: [cost]", text, "сырой фрагмент в списке")
        for banned in (
            "вхождени",
            "Вхождени",
            "диапазон",
            "Диапазон",
            "⏎",
            "±30",
            "offset",
        ):
            self.assertNotIn(banned, text, f"технический маркер {banned!r}")
        self.assertFalse(re.search(r"\[\d+:\d+\]", text), "технические диапазоны")

    @scenario_test
    async def test_20_edit_delete_rule_and_help_describes_modes(self, h) -> None:
        """Сц. 20: правка/удаление правила; после удаления правило не влияет."""
        # Управляющий кирпичик — `[cost]` внутри строки с видимым литералом:
        # при пустом значении строка остаётся (в ней есть `[main]`), поэтому
        # видно, применилось ли правило на самом деле.
        template_id, revision, bricks = await self._setup(
            h,
            content="[main]\nКонец",
            extended="[main] — [cost] итого\nКонец\nДополнительная строка",
            specs=[PLAIN_SPEC, ("cost", "Цена", "short_text", {})],
        )
        control_id = bricks["cost"]
        with h.session_factory() as session:
            created = create_conditional_part(
                session,
                actor_telegram_user_id=h.user_id,
                template_id=template_id,
                expected_revision=revision,
                section=SECTION_EXTENDED_CONTENT,
                brick_id=control_id,
                fragment=" — [cost] итого",
                occurrence=1,
            )
            self.assertTrue(created.ok, created.message)
        empty_cost = {bricks["main"]: "Концерт", control_id: ""}
        with_rule = self._preview_extended(h, template_id, empty_cost, control_id)
        self.assertNotIn("[cost]", with_rule)
        self.assertNotIn("итого", with_rule, "правило не скрыло фрагмент")

        # Правка правила через интерфейс: режим «свой фрагмент».
        await _open_rules(h, template_id)
        await h.callback(f"tp_condb:{template_id}:e:{control_id}")
        await h.callback(f"tp_condf:{template_id}:custom")
        # Автор вводит фрагмент ровно как в тексте: кирпичик внутри строки.
        await h.text(" — [cost] итого")
        self.assertIn("Цена ✓", screen_labels(h), "условие сохранено и помечено")
        await h.callback(f"tp_condb:{template_id}:e:{control_id}")
        self.assertIn("свой фрагмент · выбрано", h.screen_text())
        self.assertEqual(
            {(part.fragment, part.occurrence) for part in _parts(h, template_id)},
            {(" — [cost] итого", 1)},
            "правка сохранила авторский фрагмент",
        )
        self.assertEqual(
            self._preview_extended(h, template_id, empty_cost, control_id),
            with_rule,
            "правка фрагмента не изменила рендер",
        )
        # Удаление через подтверждение.
        await h.callback(f"tp_condb:{template_id}:e:{control_id}")
        await h.callback(f"tp_condd:{template_id}:e:{control_id}")
        self.assertIn("Удалить условие?", h.screen_text())
        await h.callback(f"tp_conddy:{template_id}:e:{control_id}")
        self.assertEqual(_parts(h, template_id), [])
        self.assertIn("Цена", screen_labels(h), "условие удалено — без ✓")
        self.assertNotIn("Цена ✓", screen_labels(h))
        # Правило больше не влияет: скрытый фрагмент вернулся в анонс.
        after_delete = self._preview_extended(h, template_id, empty_cost, control_id)
        self.assertIn("итого", after_delete, "удалённое правило всё ещё скрывает")

        # §17 финального задания 2026-11-08: инструкция по условным частям —
        # только подсказка административного редактора (`templates.cond.hint`);
        # в «Помощи» отдельного раздела/инструкции нет.
        cat.assert_shows(self, h.screen_text(), "templates.cond.hint")
        from utils.rich_content import load_catalog

        help_texts: list[str] = []
        for section in load_catalog().help.sections:
            help_texts.append(section.title)
            for block in section.blocks:
                help_texts.extend(_catalog_block_texts(block))
        help_text = " ".join(help_texts)
        self.assertNotIn("всю строку", help_text)
        self.assertNotIn("разделителем", help_text)
        self.assertNotIn("свой фрагмент", help_text)
        self.assertNotIn("условн", help_text.lower())


# ===========================================================================
# Итоговый. Замороженные снимки (§8, сц. 21)
# ===========================================================================


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FrozenSnapshotTests(unittest.TestCase):
    def _preview(self, h, draft_id: int) -> str:
        with h.session_factory() as session:
            return build_announcement_preview(
                session, actor_telegram_user_id=h.user_id, draft_id=draft_id
            ).html

    def _revision(self, h, template_id: int) -> int:
        with h.session_factory() as session:
            row = session.get(Template, template_id)
            assert row is not None
            return int(row.revision)

    @scenario_test
    async def test_21_snapshot_rules_delete_edit_and_frozen_flag(self, h) -> None:
        """Сц. 21: рендер по снимку; правка/удаление живого шаблона и frozen не влияют."""
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, revision, bricks = _seed_personal_template(
            h,
            specs=[
                ("main", "Основное поле", "short_text", {}),
                ("cost", "Цена", "short_text", {}),
            ],
            content="**[main] – [cost]**\nКонец",
            extended_content="**[main] – [cost]**\nКонец",
        )
        with h.session_factory() as session:
            created = create_conditional_part(
                session,
                actor_telegram_user_id=h.user_id,
                template_id=template_id,
                expected_revision=revision,
                # Анонс без доп. полей рендерится из `content` (§43), поэтому
                # правило обязано жить в той же секции — иначе оно не влияет
                # на рендер и тест проверял бы пустоту.
                section=SECTION_CONTENT,
                brick_id=bricks["cost"],
                fragment=" – [cost]",
                occurrence=1,
            )
            self.assertTrue(created.ok, created.message)

        # Анонс создаётся ДО правок шаблона.
        with h.session_factory() as session:
            draft = start_announcement_wizard(
                session, actor_telegram_user_id=h.user_id, place_id=place_id
            )
            confirmed = confirm_announcement_place(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=draft.draft_id,
                expected_revision=draft.revision,
            )
            details = choose_announcement_template(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=confirmed.draft_id,
                template_id=template_id,
                expected_revision=confirmed.revision,
            )
            draft_id = details.draft_id
            saved = save_announcement_value(
                session,
                actor_telegram_user_id=h.user_id,
                draft_id=draft_id,
                brick_id=bricks["main"],
                value="Концерт",
                expected_revision=details.revision,
            )
            assert saved.template_snapshot is not None
            snapshot_id = saved.template_snapshot.snapshot_id
        frozen = self._preview(h, draft_id)
        self.assertEqual(frozen, "<b>Концерт</b>\nКонец", frozen)

        # Удаление живого правила и правка текста не меняют анонс.
        with h.session_factory() as session:
            rules = list_conditional_parts(session, h.user_id, template_id)
            self.assertEqual(len(rules), 1)
            deleted = delete_conditional_part(
                session,
                actor_telegram_user_id=h.user_id,
                part_id=rules[0].id,
                expected_revision=revision,
            )
            self.assertTrue(deleted.ok, deleted.message)
        with h.session_factory() as session:
            updated = update_template_content(
                session,
                actor_telegram_user_id=h.user_id,
                template_id=template_id,
                expected_revision=self._revision(h, template_id),
                content="**[main] – [cost]**\nПереписано",
                extended_content="**[main] – [cost]**\nПереписано",
            )
            self.assertTrue(updated.ok, updated.message)
        self.assertEqual(
            self._preview(h, draft_id), frozen, "живой шаблон изменил анонс"
        )

        # frozen-флаг в БД на рендер не влияет (легаси 2.5.2 §8).
        with h.session_factory() as session:
            session.execute(
                text("DROP TRIGGER IF EXISTS trg_template_snapshots_no_update")
            )
            session.execute(
                text(
                    "UPDATE template_snapshots SET conditional_rules_frozen = 0 "
                    "WHERE id = :snapshot_id"
                ),
                {"snapshot_id": int(snapshot_id)},
            )
            session.commit()
        self.assertEqual(
            self._preview(h, draft_id), frozen, "frozen=0 изменил рендер анонса"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
