# tests/test_stage253_text_fields_e2.py
"""
Poster 2.5.3 §5 — приёмка E2: текстовые поля правятся на самом анонсе.

Проверяется пользовательский результат через настоящее приложение, FakeBot и
временную SQLite (`tests/integration_harness.py`): реальная `post_bot.db`,
`.env` и сеть Telegram не используются.

Что закрепляется (все сценарии — полный путь автора в чате):

1. Готовый анонс с одним фото: нажатие «📝 Текстовые поля» (`anp_data`) НЕ
   создаёт сообщений — адрес анонса и число сообщений чата те же, клавиатура
   сообщения становится списком полей, тело/подпись перечня полей не содержат.
2. Поля площадки (название места, адрес, ссылка, город) в список не попадают,
   пользовательские поля — попадают; у личного шаблона доступны все поля.
3. Выбор поля даёт РОВНО ОДИН временный вопрос; короткий текст закрывает его,
   обновляет подпись анонса и возвращает управление (`anp_*`).
4. Медиагруппа: список полей — правка текста СУЩЕСТВУЮЩЕЙ панели, без второй
   панели и без нового сообщения.
5. Рестарт (новый harness на той же БД): `anp_data` и `anf_sel` работают по
   `instance_id`, правка сохраняется.
6. Кнопка называется «📝 Текстовые поля»; строки «📝 Данные» нет ни в одной
   пользовательской клавиатуре.
7. Пагинация списка: 5 полей на страницу при 6+, стрелок нет при ≤5.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage253_text_fields_e2 -v
"""

from __future__ import annotations

import asyncio
import shutil
import unittest
from datetime import date
from pathlib import Path

try:
    import announcement_controls as controls
    from announcement_store import FIELD_STATUS_KNOWN, build_announcement_fields
    from brick_types import SYSTEM_BRICKS_BY_CODE
    from template_store import get_standard_template, update_template_content
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        Harness,
        inline_buttons,
        scenario_test,
    )
    from tests.test_stage25_acceptance_c import _assert_field_list
    from tests.test_stage25_delivery_instances import (
        _delivered_with_photos,
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
FIELDS_TITLE = tr("buttons.finished.data")
FIELDS_HINT = tr("wizard.fields.hint")
PHOTOS_TITLE = tr("wizard.photos.title")
# 2.5.3 §5: прежней подписи кнопки больше нет.
REMOVED_LABEL = "📝 Данные"
# Формулировка, утверждённая владельцем для этапа E2 (не из каталога: тест
# обязан заметить подмену текста в YAML).
EXPECTED_FIELDS_TITLE = "📝 Текстовые поля"

# Типы вызовов, создающих новое сообщение в чате.
CREATE_TYPES = frozenset({"message", "photo", "media_group", "rich_message"})

# Системные кирпичики площадки (значения берутся из справочника, §5).
PLACE_CODES = ("место", "адрес", "ссылка_места", "город")
PLACE_TITLES = tuple(SYSTEM_BRICKS_BY_CODE[code].title for code in PLACE_CODES)

USE_TEXT = "Новое значение"


# --- помощники -------------------------------------------------------------


def _created_count(h) -> int:
    """Сколько вызовов создали сообщения бота (без правок и удалений)."""
    return sum(1 for call in h.calls if call["type"] in CREATE_TYPES)


def _created_ids(h) -> set[int]:
    """`message_id` всех сообщений, созданных ботом за сцену."""
    out: set[int] = set()
    for call in h.calls:
        if call["type"] not in CREATE_TYPES:
            continue
        out.update(int(mid) for mid in h.bot.call_message_ids.get(id(call), ()))
    return out


def _live_ids(h) -> set[int]:
    """`message_id` сообщений, которые сейчас реально есть в чате."""
    return _created_ids(h) - {int(mid) for mid in h.deleted_ids()}


def _field_buttons(h) -> list[tuple[str, str]]:
    """Живые кнопки полей списка (подпись, `anf_sel:` callback)."""
    return [
        (label, data)
        for label, data in h.live_buttons()
        if data and data.startswith("anf_sel:")
    ]


def _field_labels(h) -> str:
    """Склейка подписей кнопок полей — проверка «поле есть/нет в списке»."""
    return " | ".join(label for label, _data in _field_buttons(h))


def _page_callbacks(h) -> list[str]:
    return h.live_callbacks("anf_page:")


def _field_list_body() -> str:
    """Ожидаемое тело списка: заголовок и подсказка, без перечня полей."""
    return f"{FIELDS_TITLE}\n\n{FIELDS_HINT}"


async def _open_field_list(h, instance_id: int) -> None:
    await h.callback(controls.panel_callback(controls.ACTION_DATA, instance_id))


async def _delivered_personal(h, *, fields: int, photos: int = 0):
    """
    Личный шаблон с `fields` короткими текстовыми полями и доставка.

    `photos` — 0 («Без фотографий»), 1 (одно фото) или 2 (альбом).
    """
    place_id, _alias_id = _seed_place(h)
    specs = [
        (f"f{index}", f"Поле {index}", "short_text", {}) for index in range(fields)
    ]
    content = "\n".join(f"Строка {index}: [f{index}]" for index in range(fields))
    template_id, _revision, _bricks = _seed_personal_template(
        h,
        name=f"Шаблон на {fields} полей",
        specs=specs,
        content=content,
    )
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    # Отвечаем на вопросы подряд, пока не откроется шаг фотографий.
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
        # Альбом закрывается по паузе приёма (как в настоящем Telegram).
        await asyncio.sleep(0.4)
    return _latest_draft(h)


async def _delivered_standard_with_city(h):
    """
    Стандарт площадки с добавленным `[город]`: в анонсе все поля площадки.

    Без `[город]` в шаблоне его отсутствие в списке ничего не доказывает:
    проверка должна видеть, что поле реально есть в анонсе и всё равно
    скрыто из списка текстовых полей.
    """
    place_id, _alias_id = _seed_place(h)
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
    for _ in range(20):
        skips = [
            data
            for _label, data in h.screen_buttons()
            if data and data.startswith("aw_qskip")
        ]
        if not skips:
            break
        await h.callback(skips[0])
    if PHOTOS_TITLE in h.screen_text():
        await h.callback(_callback(h, "aw_photo_none"))
    return _latest_draft(h)


def _known_field_codes(h, draft_id: int) -> set[str]:
    """Коды полей анонса со статусом «из справочника» (значение площадки)."""
    with h.session_factory() as session:
        fields = build_announcement_fields(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
            today=date.today(),
        )
    return {field.code for field in fields if field.status == FIELD_STATUS_KNOWN}


async def _restart_with_new_harness(h) -> Harness:
    """
    Новый harness на той же БД — рестарт службы с живым чатом.

    FakeBot переиспользуется: сообщения чата принадлежат Telegram и после
    рестарта процесса остаются на месте; теряются только `user_data` и
    состояние диалога.
    """
    snapshot = Path(h._tmp.name) / "restart-copy.db"
    h.engine.dispose()
    shutil.copyfile(Path(h._tmp.name) / "stage9.db", snapshot)
    fresh = Harness()
    fresh.bot = h.bot
    fresh.engine.dispose()
    shutil.copyfile(snapshot, Path(fresh._tmp.name) / "stage9.db")
    await fresh.start()
    return fresh


# --- 1. готовый анонс: список полей без новых сообщений ---------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FieldListOnAnnouncementTests(unittest.TestCase):
    """§5: список полей живёт на самом управлении анонса, не отдельным экраном."""

    @scenario_test
    async def test_one_photo_field_list_creates_no_messages(self, h) -> None:
        """(а) «📝 Текстовые поля» у фото-анонса: 0 новых сообщений."""
        await h.text("/start")
        draft = await _delivered_personal(h, fields=5, photos=1)
        active = _only_active(h, draft.id)
        message_id = active.message_ids[0]
        stored = _stored(h, draft.id)
        caption_before = h.bot.content_text(h.chat_id, message_id)
        self.assertEqual(caption_before, stored.publication_text)

        created_before = _created_count(h)
        live_before = _live_ids(h)
        calls_before = len(h.calls)

        await _open_field_list(h, active.id)

        # Ни одного нового сообщения и никаких правок содержимого анонса.
        self.assertEqual(_created_count(h), created_before, "сообщений не прибавилось")
        self.assertEqual(_live_ids(h), live_before, "чат не изменился")
        self.assertEqual(
            _only_active(h, draft.id).message_ids,
            (message_id,),
            "message_id анонса не изменился",
        )
        self.assertEqual(_stored(h, draft.id).delivered_message_ids, (message_id,))
        self.assertIn(
            (h.chat_id, message_id),
            h.bot._message_content,
            "сообщение анонса осталось в чате",
        )
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            caption_before,
            "подпись анонса не переписана перечнем полей",
        )

        # Клавиатура сообщения — список полей: выбор, «Назад».
        actions_since = {call["type"] for call in h.calls[calls_before:]}
        self.assertNotIn("edit_caption", actions_since)
        self.assertNotIn("edit_media", actions_since)
        self.assertNotIn("edit_message", actions_since, "тело анонса не трогается")
        self.assertTrue(_field_buttons(h), f"нет списка полей: {h.live_buttons()}")
        # Список полей целиком: `anf_sel:` (поля) и `anf_back:` (возврат);
        # `anf_page:` у 5 полей не нужен, `anp_` скрыты.
        _assert_field_list(h)
        self.assertEqual(
            h.live_label(_field_buttons(h)[0][1]),
            "1. Поле 0 — заполнено",
            "список полей — на клавиатуре анонса",
        )
        self.assertEqual(_page_callbacks(h), [], "у 5 полей ещё нет второй страницы")
        self.assertIsNone(h.screen_message_id(), "отдельного экрана нет")

        # Тело без перечня полей текстом: сообщений с текстом не появилось.
        texts_since = [
            call.get("text")
            for call in h.calls[calls_before:]
            if call["type"] in {"message", "edit_message", "photo", "rich_message"}
            and call.get("text") is not None
        ]
        self.assertEqual(texts_since, [], "перечень полей не уходит текстом")

    # --- 2. поля площадки скрыты, пользовательские доступны ----------------

    @scenario_test
    async def test_place_fields_are_not_editable(self, h) -> None:
        """(б) поля площадки в списке отсутствуют, пользовательские — есть."""
        await h.text("/start")
        draft = await _delivered_standard_with_city(h)

        known_codes = _known_field_codes(h, draft.id)
        self.assertLessEqual(
            set(PLACE_CODES),
            known_codes,
            "поля площадки действительно есть в анонсе (значения из справочника)",
        )

        active = _only_active(h, draft.id)
        await _open_field_list(h, active.id)
        _assert_field_list(h)
        labels = _field_labels(h)
        for title in PLACE_TITLES:
            self.assertNotIn(title, labels, f"поле площадки {title!r} в списке")
        for title in ("Название мероприятия", "Описание", "Контакт"):
            self.assertIn(title, labels, f"пользовательского поля {title!r} нет")

    @scenario_test
    async def test_personal_template_fields_are_editable(self, h) -> None:
        """(б) у личного шаблона все его поля доступны для правки."""
        await h.text("/start")
        draft = await _delivered_personal(h, fields=3, photos=0)
        active = _only_active(h, draft.id)

        await _open_field_list(h, active.id)
        _assert_field_list(h)
        labels = _field_labels(h)
        for index in range(3):
            self.assertIn(f"Поле {index}", labels, f"нет поля {index}: {labels!r}")
        for title in PLACE_TITLES:
            self.assertNotIn(title, labels)
        self.assertEqual(len(_field_buttons(h)), 3)
        self.assertEqual(_page_callbacks(h), [], "у 3 полей стрелок нет")

    # --- 3. выбор поля: один вопрос, подпись, возврат управления -----------

    @scenario_test
    async def test_field_edit_closes_question_and_updates_caption(self, h) -> None:
        """(в) поле → ровно один вопрос → текст → caption и управление."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 1)
        active = _only_active(h, draft.id)
        message_id = active.message_ids[0]
        await _open_field_list(h, active.id)

        created_before = _created_count(h)
        await h.callback(_field_buttons(h)[0][1])

        # Ровно одно новое сообщение — временный вопрос.
        self.assertEqual(
            _created_count(h),
            created_before + 1,
            "выбор поля даёт ровно один вопрос",
        )
        question_id = h.screen_message_id()
        self.assertIsNotNone(question_id, "вопрос не показан")
        self.assertNotEqual(question_id, message_id)
        self.assertIn(
            tr("wizard.question.header", step=1, total=1, title="Основное поле"),
            h.screen_text(),
        )

        created_at_question = _created_count(h)
        await h.text(USE_TEXT)

        # Вопрос закрыт/удалён, второго вопроса нет.
        self.assertEqual(
            _created_count(h), created_at_question, "лишних сообщений при правке нет"
        )
        self.assertIn(question_id, h.deleted_ids(), "временный вопрос не закрыт")
        self.assertIsNone(h.screen_message_id(), "временный экран остался")
        self.assertEqual(_field_buttons(h), [], "список полей остался висеть")

        # Подпись анонса обновилась на месте.
        self.assertEqual(h.bot.content_text(h.chat_id, message_id), USE_TEXT)
        edited = _stored(h, draft.id)
        self.assertEqual(edited.publication_text, USE_TEXT)
        self.assertEqual(edited.delivered_message_ids, (message_id,))
        self.assertEqual(_only_active(h, draft.id).message_ids, (message_id,))

        # Управление вернулось на сам анонс (`anp_*`).
        buttons = inline_buttons(_final_markup(h, message_id))
        self.assertTrue(buttons, "у анонса нет управления после правки")
        self.assertEqual(
            [data for _label, data in buttons],
            [
                controls.panel_callback(action, active.id)
                for action in controls.PANEL_ACTIONS
            ],
            "управление анонсом вернулось целиком",
        )
        self.assertFalse(h.live_callbacks("anf_"), "список полей не скрыт")

    # --- 4. медиагруппа: список полей в существующей панели ----------------

    @scenario_test
    async def test_album_field_list_edits_existing_panel(self, h) -> None:
        """(г) альбом: `editMessageText` существующей панели, второй нет."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 2)
        active = _only_active(h, draft.id)
        panel_id = active.panel_message_id
        self.assertIsNotNone(panel_id, "у альбома нет панели")
        sends_before = len(_panel_sends(h))
        created_before = _created_count(h)

        await _open_field_list(h, active.id)

        self.assertEqual(_created_count(h), created_before, "новых сообщений нет")
        self.assertEqual(
            len(_panel_sends(h)), sends_before, "вторая панель не создавалась"
        )
        # `_panel_records` видит и отправку панели, и её правку: в чате одна
        # панель, поэтому создана ровно одна и правится та же.
        self.assertEqual(len(_panel_records(h)), sends_before + 1)
        last = _panel_records(h)[-1]
        self.assertEqual(last["type"], "edit_message", "панель именно правится")
        self.assertEqual(int(last["message_id"]), panel_id, "правится та же панель")
        self.assertEqual(last["text"], _field_list_body())
        self.assertEqual(
            _only_active(h, draft.id).panel_message_id,
            panel_id,
            "адрес панели не изменился",
        )
        self.assertTrue(_field_buttons(h), f"списка полей в панели нет: {last}")
        _assert_field_list(h)
        self.assertIsNone(h.screen_message_id(), "служебного экрана нет")

    # --- 5. рестарт: callbacks работают по instance_id ----------------------

    @scenario_test
    async def test_after_restart_callbacks_work_by_instance(self, h) -> None:
        """(д) новый harness на той же БД: список и правка по id экземпляра."""
        await h.text("/start")
        draft = await _delivered_with_photos(h, 1)
        active = _only_active(h, draft.id)
        instance_id = active.id
        message_id = active.message_ids[0]

        fresh = await _restart_with_new_harness(h)
        try:
            # Ни user_data, ни состояния диалога после рестарта нет.
            self.assertIsNone(
                fresh.user_data().get("announcement_ui"),
                "состояние диалога должно потеряться",
            )
            await _open_field_list(fresh, instance_id)
            _assert_field_list(fresh)
            self.assertTrue(_field_buttons(fresh), "список полей не открылся")

            await fresh.callback(_field_buttons(fresh)[0][1])
            self.assertIsNotNone(fresh.screen_message_id(), "вопрос не открылся")
            await fresh.text(USE_TEXT)

            stored = _stored(fresh, draft.id)
            self.assertEqual(stored.publication_text, USE_TEXT)
            self.assertEqual(stored.delivered_message_ids, (message_id,))
            self.assertEqual(
                fresh.bot.content_text(fresh.chat_id, message_id),
                USE_TEXT,
                "правка после рестарта применена к тому же сообщению",
            )
            self.assertEqual(
                _only_active(fresh, draft.id).id, instance_id, "тот же экземпляр"
            )
        finally:
            await fresh.close()

    # --- 6. подпись кнопки ---------------------------------------------------

    @scenario_test
    async def test_text_fields_button_replaces_data_label(self, h) -> None:
        """(е) «📝 Текстовые поля» вместо «📝 Данные» во всех клавиатурах."""
        self.assertEqual(FIELDS_TITLE, EXPECTED_FIELDS_TITLE)
        # Сама фабрика клавиатуры (`panel_keyboard`) даёт новую подпись.
        keyboard_labels = [
            button.text
            for row in controls.panel_keyboard(1, photos_count=0).inline_keyboard
            for button in row
        ]
        self.assertEqual(keyboard_labels[0], FIELDS_TITLE, keyboard_labels)
        self.assertNotIn(REMOVED_LABEL, keyboard_labels)

        await h.text("/start")
        draft = await _delivered_with_photos(h, 2)
        active = _only_active(h, draft.id)
        panel_markup = _panel_records(h)[-1]["reply_markup"]
        panel_labels = [label for label, _data in inline_buttons(panel_markup)]
        self.assertEqual(panel_labels[0], FIELDS_TITLE, panel_labels)
        self.assertNotIn(REMOVED_LABEL, panel_labels)

        # `panel_keyboard` продукта: набор действий управления — прежний.
        self.assertEqual(
            [data for _label, data in inline_buttons(panel_markup)],
            [
                controls.panel_callback(action, active.id)
                for action in controls.PANEL_ACTIONS
            ],
        )
        self.assertNotIn(
            REMOVED_LABEL,
            [label for label, _data in h.live_buttons()],
            "живые клавиатуры не содержат «📝 Данные»",
        )

        # После успешной правки подпись тоже новая: правка возвращает `anp_`.
        await _open_field_list(h, active.id)
        await h.callback(_field_buttons(h)[0][1])
        await h.text(USE_TEXT)
        labels_after = [label for label, _data in h.live_buttons()]
        self.assertIn(FIELDS_TITLE, labels_after)
        self.assertNotIn(REMOVED_LABEL, labels_after)

    # --- 7. пагинация списка полей ------------------------------------------

    @scenario_test
    async def test_pagination_five_fields_per_page(self, h) -> None:
        """7 полей — две страницы по 5; 5 полей — без стрелок (subTest)."""
        await h.text("/start")
        for fields in (7, 5):
            with self.subTest(fields=fields):
                draft = await _delivered_personal(h, fields=fields, photos=0)
                active = _only_active(h, draft.id)
                await _open_field_list(h, active.id)

                self.assertEqual(
                    len(_field_buttons(h)),
                    min(fields, 5),
                    f"на странице {min(fields, 5)} полей",
                )
                if fields <= 5:
                    self.assertEqual(_page_callbacks(h), [], "стрелок нет")
                    self.assertEqual(h.live_callbacks("noop:"), [], "счётчика нет")
                else:
                    self.assertEqual(_page_callbacks(h), [f"anf_page:{active.id}:5"])
                    self.assertEqual(
                        h.live_callbacks("noop:"), ["noop:1:2"], "индикатор 1/2"
                    )
                    self.assertEqual(
                        [
                            label
                            for label, data in h.live_buttons()
                            if data == "noop:1:2"
                        ],
                        ["1/2"],
                    )
                    await h.callback(_page_callbacks(h)[0])
                    self.assertEqual(
                        len(_field_buttons(h)), 2, "вторая страница: 2 поля"
                    )
                    self.assertEqual(_page_callbacks(h), [f"anf_page:{active.id}:0"])
                    self.assertEqual(h.live_callbacks("noop:"), ["noop:2:2"])
                    await h.callback(_page_callbacks(h)[0])
                    self.assertEqual(len(_field_buttons(h)), 5, "возврат на первую")

                # Следующий кейс начинается с чистого экрана меню.
                await h.callback(h.live_callbacks("anf_back:")[0])
                await h.callback("main_menu")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
