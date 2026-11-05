# tests/test_stage253_acceptance_e2.py
"""
Poster 2.5.3 §5 — приёмка: текстовые поля правятся на самом анонсе.

Сквозные пользовательские сценарии через настоящее приложение, FakeBot и
временную SQLite (`tests/integration_harness.py`): реальная `post_bot.db`,
`.env` и сеть Telegram не используются.

Что закрепляет приёмка (в отличие от `test_stage253_text_fields_e2`, который
проверяет отдельные свойства, здесь — требования владельца целиком):

D. «Текстовые поля на самом анонсе»
   D1. У доставленного анонса в клавиатуре есть «📝 Текстовые поля», а
       экрана и кнопки «✏️ Изменить анонс» нет нигде в чате.
   D2. Поля площадки (название места, адрес, ссылка, город) не редактируются
       списком, даже если значение пустое (фильтр по коду, а не по статусу).
   D3. Регрессия D3: 7 полей → страница 2 → последнее поле → «◀️ Назад»
       возвращает именно на страницу 2 списка (а не на первый вопрос).
   D4. Простой и расширенный анонс идут одним и тем же путём; в интерфейсе
       нет «основного/расширенного текста» как названий разделов.
   D5. Сохранённое значение сразу видно в анонсе и переживает рестарт.

K. Негативный сценарий: подделанная навигация вопроса
   K1. Поддельный `aw_qgoto` с brick_id адреса после открытия реального
       текстового поля получает отказ, и значение адреса не появляется ни в
       `announcement_values`, ни в тексте анонса (проверка состоянием БД).
   K2. То же через resume-снимок после рестарта: текст не записывается в
       не-текстовое поле площадки; легитимный resume (по описанию) работает.
   K3. Легитимный путь: поле открывается из списка, значение сохраняется в
       анонсе, адрес остаётся нетронутым.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage253_acceptance_e2 -v
"""

from __future__ import annotations

import json
import unittest

try:
    from sqlalchemy import text, update

    import announcement_controls as controls
    from announcement_store import (
        FIELD_STATUS_KNOWN,
        build_announcement_fields,
        set_announcement_stage,
    )
    from announcement_wizard import WizardStage
    from brick_types import SYSTEM_BRICKS_BY_CODE
    from models import UserState
    from template_store import get_standard_template, update_template_content
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.test_stage25_acceptance_c import _assert_field_list
    from tests.test_stage25_delivery_instances import _only_active, _stored
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


# Подписи — из каталога текстов, а не литералами.
FIELDS_TITLE = tr("buttons.finished.data")
PHOTOS_TITLE = tr("wizard.photos.title")
STALE_TEXT = tr("errors.stale.wizard")
MENU_HINT = tr("menu.outside.text")

# 2.5.3 §5: экрана и кнопки «✏️ Изменить анонс» больше нет.
REMOVED_TITLE = "✏️ Изменить анонс"

# Системные кирпичики площадки (значения берутся из справочника, §5).
PLACE_CODES = ("место", "адрес", "ссылка_места", "город")
PLACE_TITLES = tuple(SYSTEM_BRICKS_BY_CODE[code].title for code in PLACE_CODES)

NEW_VALUE = "Новое значение поля"
ADDRESS_SENTINEL = "ЗАГЛУШКА-АДРЕС"
DESCRIPTION_VALUE = "ОПИСАНИЕ-ИЗ-RESUME"

CREATE_TYPES = frozenset({"message", "photo", "media_group", "rich_message"})


# --- помощники -------------------------------------------------------------


def _created_count(h) -> int:
    """Сколько вызовов создали сообщения бота (без правок и удалений)."""
    return sum(1 for call in h.calls if call["type"] in CREATE_TYPES)


def _texts_since(h, index: int) -> list[str]:
    """Тексты сообщений/правок, созданных начиная с вызова `index`."""
    out: list[str] = []
    for call in h.calls[index:]:
        if call["type"] not in {
            "message",
            "edit_message",
            "photo",
            "rich_message",
            "media_group",
        }:
            continue
        text = call.get("text")
        if isinstance(text, str) and text:
            out.append(text)
    return out


def _answers_since(h, index: int) -> list[dict]:
    return [
        call
        for call in h.calls[index:]
        if call["type"] == "callback_answer"
    ]


def _field_buttons(h) -> list[tuple[str, str]]:
    """Живые кнопки полей списка (подпись, `anf_sel:` callback)."""
    return [
        (label, data)
        for label, data in h.live_buttons()
        if data and data.startswith("anf_sel:")
    ]


def _field_labels(h) -> str:
    return " | ".join(label for label, _data in _field_buttons(h))


def _all_labels(h) -> str:
    return " | ".join(label for label, _data in h.live_buttons())


async def _open_field_list(h, instance_id: int) -> None:
    await h.callback(controls.panel_callback(controls.ACTION_DATA, instance_id))


async def _delivered_personal(h, *, fields: int, photos: int, extended: bool = False):
    """Личный шаблон с `fields` полями → вопросы → шаг фото → доставка."""
    place_id, _alias = _seed_place(h)
    specs = [
        (f"f{index}", f"Поле {index}", "short_text", {}) for index in range(fields)
    ]
    content = "\n".join(f"Строка {index}: [f{index}]" for index in range(fields))
    template_id, _revision, bricks = _seed_personal_template(
        h,
        name=f"Шаблон на {fields} полей",
        specs=specs,
        content=content,
        extended_content=content if extended else None,
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
    else:
        await h.photo("photo-0")
    draft = _latest_draft(h)
    if _stored(h, draft.id).publication_status != "sent":
        raise AssertionError("анонс не доставлен")
    return draft, bricks


async def _delivered_standard(h, *, with_city: bool = False):
    """Стандарт площадки с пустым адресом; все вопросы пропущены."""
    place_id, _alias = _seed_place(h, address=None)
    if with_city:
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
    if with_city:
        await h.callback(_callback(h, "aw_template", f":{standard_id}"))
    else:
        await h.callback(_callback(h, "aw_template"))
    for _ in range(30):
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
    draft = _latest_draft(h)
    if _stored(h, draft.id).publication_status != "sent":
        raise AssertionError(
            f"анонс не доставлен: {_stored(h, draft.id).publication_status}"
        )
    return draft


def _draft_row(h, draft_id: int):
    """(stage, revision, publication_status, publication_text) из БД."""
    with h.session_factory() as session:
        return session.execute(
            text(
                "SELECT stage, revision, publication_status, publication_text "
                "FROM announcement_drafts WHERE id = :draft_id"
            ),
            {"draft_id": int(draft_id)},
        ).first()


def _value_rows(h, draft_id: int) -> list[tuple[int, str, str | None]]:
    """Все строки `announcement_values` черновика (brick_id, state, value_json)."""
    with h.session_factory() as session:
        rows = session.execute(
            text(
                "SELECT brick_id, state, value_json FROM announcement_values "
                "WHERE draft_id = :draft_id ORDER BY brick_id"
            ),
            {"draft_id": int(draft_id)},
        ).all()
    return [(int(row[0]), str(row[1]), row[2]) for row in rows]


def _field_map(h, draft_id: int) -> dict[str, object]:
    """Поля анонса по коду кирпичика (для проверки «поле есть в анонсе»)."""
    with h.session_factory() as session:
        fields = build_announcement_fields(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=int(draft_id),
        )
    return {field.code: field for field in fields}


def _place_brick_id(code: str) -> int:
    return SYSTEM_BRICKS_BY_CODE[code].id


# --- D. Текстовые поля на самом анонсе ------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TextFieldsOnAnnouncementAcceptanceTests(unittest.TestCase):
    """2.5.3 §5: текстовые поля правятся на самом анонсе."""

    # --- D1 ---------------------------------------------------------------
    @scenario_test
    async def test_d1_button_on_announcement_and_no_edit_screen(self, h) -> None:
        """D1: «📝 Текстовые поля» есть, «✏️ Изменить анонс» — нигде."""
        await h.text("/start")
        draft, _bricks = await _delivered_personal(h, fields=2, photos=1)
        active = _only_active(h, draft.id)

        labels = _all_labels(h)
        self.assertIn(FIELDS_TITLE, labels, f"нет кнопки поля: {labels!r}")
        self.assertNotIn(REMOVED_TITLE, labels, f"старая кнопка в чате: {labels!r}")
        self.assertEqual(
            h.live_callbacks("aw_edit"), [], "старой кнопки правки нет в чате"
        )
        # Ни одного пользовательского текста с названием удалённого экрана.
        for text_value in _texts_since(h, 0):
            self.assertNotIn(REMOVED_TITLE, text_value, "удалённый экран показан")

        # Устаревшая кнопка прежней версии не воскрешает экран: она попадает
        # в актуальный список полей (§5), а не в «✏️ Изменить анонс».
        calls_before = len(h.calls)
        revision = _draft_row(h, draft.id)[1]
        await h.callback(f"aw_edit:{draft.id}:{revision}")

        self.assertEqual(
            _only_active(h, draft.id).message_ids,
            active.message_ids,
            "message_id анонса не изменился",
        )
        _assert_field_list(h)
        for text_value in _texts_since(h, calls_before):
            self.assertNotIn(REMOVED_TITLE, text_value, "экран-призрак вернулся")

    # --- D2 ---------------------------------------------------------------
    @scenario_test
    async def test_d2_place_fields_are_not_editable(self, h) -> None:
        """D2: адрес/место/ссылка/город не редактируются даже без значения."""
        await h.text("/start")
        draft = await _delivered_standard(h, with_city=True)

        field_map = _field_map(h, draft.id)
        for code in PLACE_CODES:
            self.assertIn(code, field_map, f"поля площадки {code!r} нет в анонсе")
        # Адрес пропущен автором: он не `known`, и всё равно скрыт — фильтр
        # по коду, а не по статусу значения.
        self.assertNotEqual(
            field_map["адрес"].status,
            FIELD_STATUS_KNOWN,
            "адрес должен быть в анонсе без значения",
        )
        for code in ("место", "ссылка_места", "город"):
            self.assertEqual(
                field_map[code].status,
                FIELD_STATUS_KNOWN,
                f"значение {code!r} пришло из справочника",
            )

        active = _only_active(h, draft.id)
        await _open_field_list(h, active.id)
        _assert_field_list(h)
        labels = _field_labels(h)
        for title in PLACE_TITLES:
            self.assertNotIn(title, labels, f"поле площадки {title!r} в списке")
        for title in ("Название мероприятия", "Описание", "Контакт"):
            self.assertIn(title, labels, f"текстового поля {title!r} нет в списке")

    # --- D3 ---------------------------------------------------------------
    @scenario_test
    async def test_d3_back_from_last_field_returns_to_second_page(self, h) -> None:
        """D3: 7 полей → страница 2 → последнее поле → «Назад» на страницу 2."""
        await h.text("/start")
        draft, bricks = await _delivered_personal(h, fields=7, photos=1)
        active = _only_active(h, draft.id)
        await _open_field_list(h, active.id)

        # Страница 1: пять полей и стрелка на вторую.
        self.assertEqual(len(_field_buttons(h)), 5, "первая страница: 5 полей")
        self.assertEqual(
            h.live_callbacks("anf_page:"),
            [f"anf_page:{active.id}:5"],
            "стрелка вперёд",
        )
        await h.callback(h.live_callbacks("anf_page:")[0])

        # Страница 2: оставшиеся два поля, стрелка назад — на первую.
        self.assertEqual(len(_field_buttons(h)), 2, "вторая страница: 2 поля")
        self.assertEqual(
            h.live_callbacks("anf_page:"),
            [f"anf_page:{active.id}:0"],
            "стрелка назад",
        )
        last_data = _field_buttons(h)[-1][1]
        self.assertEqual(
            last_data,
            f"anf_sel:{active.id}:{bricks['f6']}",
            "последнее поле страницы 2",
        )
        self.assertIn("7. Поле 6", _field_labels(h))

        # Открываем последнее поле — вопрос, затем «◀️ Назад».
        await h.callback(last_data)
        self.assertIn("Вопрос 7 из 7", h.screen_text(), h.screen_text()[:200])
        backs = [
            data
            for _label, data in h.screen_buttons()
            if data and data.startswith("aw_qback")
        ]
        self.assertEqual(len(backs), 1, f"нет «Назад» вопроса: {h.screen_buttons()}")
        await h.callback(backs[0])

        # Возврат именно на страницу 2 списка (регрессия D3), а не на вопрос.
        self.assertIsNone(h.screen_message_id(), "временный вопрос остался")
        self.assertEqual(
            h.live_callbacks("anf_page:"),
            [f"anf_page:{active.id}:0"],
            "вернулись не на страницу 2",
        )
        self.assertEqual(len(_field_buttons(h)), 2, "страница 2 не восстановлена")
        self.assertIn("6. Поле 5", _field_labels(h))
        self.assertIn("7. Поле 6", _field_labels(h))
        self.assertTrue(h.live_callbacks(f"anf_back:{active.id}"))

    # --- D4 ---------------------------------------------------------------
    async def _assert_same_field_path(self, h, *, extended: bool) -> None:
        """Один и тот же путь правки для простого и расширенного анонса."""
        await h.text("/start")
        draft, bricks = await _delivered_personal(
            h, fields=2, photos=0, extended=extended
        )
        active = _only_active(h, draft.id)
        calls_before = len(h.calls)

        await _open_field_list(h, active.id)
        _assert_field_list(h)
        self.assertEqual(
            [data for _label, data in _field_buttons(h)],
            [
                f"anf_sel:{active.id}:{bricks['f0']}",
                f"anf_sel:{active.id}:{bricks['f1']}",
            ],
            "протокол списка полей одинаков",
        )
        self.assertTrue(h.live_callbacks(f"anf_back:{active.id}"))

        # «Основной/расширенный текст» — не названия разделов редактора.
        for text_value in [*_texts_since(h, calls_before), _all_labels(h)]:
            lowered = text_value.lower()
            self.assertNotIn("основной текст", lowered, "раздел «основной текст»")
            self.assertNotIn("расширенный текст", lowered, "раздел «расширенный текст»")

        await h.callback(_field_buttons(h)[0][1])
        self.assertIn("Вопрос 1 из 2", h.screen_text(), h.screen_text()[:200])
        await h.text(NEW_VALUE)

        stored = _stored(h, draft.id)
        self.assertIn(NEW_VALUE, stored.publication_text, "значение не попало в анонс")
        self.assertEqual(
            h.bot.content_text(h.chat_id, active.message_ids[0]),
            stored.publication_text,
            "показанный анонс не совпадает с сохранённым",
        )
        self.assertIn(FIELDS_TITLE, _all_labels(h), "управление не вернулось")

    @scenario_test
    async def test_d4_simple_announcement_field_path(self, h) -> None:
        """D4: простой анонс — тот же путь «список → вопрос → сохранение»."""
        await self._assert_same_field_path(h, extended=False)

    @scenario_test
    async def test_d4_extended_announcement_field_path(self, h) -> None:
        """D4: анонс с расширенной версией шаблона — тот же путь."""
        await self._assert_same_field_path(h, extended=True)

    # --- D5 ---------------------------------------------------------------
    @scenario_test
    async def test_d5_saved_value_visible_and_kept_after_restart(self, h) -> None:
        """D5: сохранённое значение сразу в анонсе и после рестарта."""
        await h.text("/start")
        draft, bricks = await _delivered_personal(h, fields=2, photos=1)
        active = _only_active(h, draft.id)
        message_id = active.message_ids[0]
        await _open_field_list(h, active.id)

        field_data = f"anf_sel:{active.id}:{bricks['f0']}"
        await h.callback(field_data)
        await h.text(NEW_VALUE)

        # Сразу видно в чате и совпадает с сохранённым текстом.
        stored = _stored(h, draft.id)
        self.assertIn(NEW_VALUE, stored.publication_text)
        self.assertEqual(h.bot.content_text(h.chat_id, message_id), stored.publication_text)

        # Рестарт службы: сообщение чата и значение остаются.
        await h.restart()
        self.assertIsNone(
            h.user_data().get("announcement_ui"), "состояние диалога должно потеряться"
        )
        self.assertEqual(
            h.bot.content_text(h.chat_id, message_id),
            stored.publication_text,
            "после рестарта подпись анонса изменилась",
        )
        self.assertEqual(
            _stored(h, draft.id).publication_text,
            stored.publication_text,
            "после рестарта значение в БД изменилось",
        )

        # Правка после рестарта открывается и видит сохранённое значение.
        await _open_field_list(h, active.id)
        _assert_field_list(h)
        label = h.live_label(field_data)
        self.assertIsNotNone(label, f"поле не найдено: {h.live_buttons()}")
        self.assertIn("заполнено", label, f"сохранённое значение не видно: {label!r}")

    # --- K. Негативный сценарий: подделанная навигация ---------------------

    @scenario_test
    async def test_k1_forged_qgoto_to_address_is_rejected(self, h) -> None:
        """K1: поддельный `aw_qgoto` на адрес — отказ, значение не записано."""
        await h.text("/start")
        draft = await _delivered_standard(h)
        active = _only_active(h, draft.id)
        await _open_field_list(h, active.id)

        # Открываем РЕАЛЬНОЕ текстовое поле из списка.
        await h.callback(_field_buttons(h)[0][1])
        self.assertIn("Вопрос", h.screen_text())

        before_row = _draft_row(h, draft.id)
        before_values = _value_rows(h, draft.id)
        revision = before_row[1]
        address_id = _place_brick_id("адрес")
        calls_before = len(h.calls)

        # Подделка: адрес — поле площадки, а не поле редактора (§5).
        await h.callback(f"aw_qgoto:{draft.id}:{revision}:{address_id}")

        answers = _answers_since(h, calls_before)
        self.assertTrue(
            any(a.get("text") == STALE_TEXT and a.get("show_alert") for a in answers),
            f"нет предупреждения об устаревании: {answers}",
        )
        self.assertEqual(
            _draft_row(h, draft.id),
            before_row,
            "поддельный переход изменил черновик",
        )
        self.assertEqual(
            _value_rows(h, draft.id),
            before_values,
            "поддельный переход записал значение",
        )
        address_rows = [row for row in _value_rows(h, draft.id) if row[0] == address_id]
        self.assertEqual(len(address_rows), 1, "у адреса должна быть одна строка")
        self.assertIsNone(address_rows[0][2], "значение адреса появилось")

        # Ввод текста после отказа тоже не пишет значение в поле площадки.
        await h.text(ADDRESS_SENTINEL)
        self.assertEqual(
            _value_rows(h, draft.id),
            before_values,
            "текст после отказа записал значение справочника",
        )
        stored = _stored(h, draft.id)
        self.assertNotIn(ADDRESS_SENTINEL, stored.publication_text or "")
        self.assertEqual(_draft_row(h, draft.id), before_row)

    @scenario_test
    async def test_k2_resume_cannot_write_place_value(self, h) -> None:
        """K2: resume-снимок на адрес отклоняется; resume по описанию — нет."""
        await h.text("/start")
        draft = await _delivered_standard(h)

        # Снимок «экран ждёт ввода» по brick_id адреса, как после рестарта.
        with h.session_factory() as session:
            set_announcement_stage(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
                stage=WizardStage.QUESTIONS,
                expected_revision=_draft_row(h, draft.id)[1],
            )
        address_id = _place_brick_id("адрес")
        with h.session_factory() as session:
            session.execute(
                update(UserState).values(
                    payload=json.dumps(
                        {
                            "announcement_resume": {
                                "mode": "text",
                                "draft_id": draft.id,
                                "brick_id": address_id,
                                "source": "question",
                            }
                        }
                    )
                )
            )
            session.commit()

        await h.restart()
        before_row = _draft_row(h, draft.id)
        before_values = _value_rows(h, draft.id)
        await h.text(ADDRESS_SENTINEL)

        self.assertEqual(
            _value_rows(h, draft.id),
            before_values,
            "resume-снимок записал значение в поле площадки",
        )
        address_rows = [row for row in _value_rows(h, draft.id) if row[0] == address_id]
        self.assertTrue(address_rows)
        self.assertIsNone(address_rows[0][2], "значение адреса появилось")
        stored = _stored(h, draft.id)
        self.assertNotIn(ADDRESS_SENTINEL, stored.publication_text or "")
        self.assertEqual(
            _draft_row(h, draft.id)[1],
            before_row.revision,
            "отклонённый resume изменил revision черновика",
        )
        self.assertIn(MENU_HINT, h.screen_text() or "", h.screen_text()[:200])

        # Контроль: тот же механизм для настоящего текстового поля работает —
        # отказ адресу не является «resume сломан целиком».
        with h.session_factory() as session:
            set_announcement_stage(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
                stage=WizardStage.QUESTIONS,
                expected_revision=_draft_row(h, draft.id)[1],
            )
        description_id = _place_brick_id("описание")
        with h.session_factory() as session:
            session.execute(
                update(UserState).values(
                    payload=json.dumps(
                        {
                            "announcement_resume": {
                                "mode": "text",
                                "draft_id": draft.id,
                                "brick_id": description_id,
                                "source": "question",
                            }
                        }
                    )
                )
            )
            session.commit()
        await h.restart()
        await h.text(DESCRIPTION_VALUE)

        description_rows = [
            row for row in _value_rows(h, draft.id) if row[0] == description_id
        ]
        self.assertTrue(description_rows, "текстовое поле не записалось")
        self.assertIn(DESCRIPTION_VALUE, description_rows[0][2] or "")
        self.assertIn(DESCRIPTION_VALUE, _stored(h, draft.id).publication_text or "")
        address_after = [row for row in _value_rows(h, draft.id) if row[0] == address_id]
        self.assertIsNone(address_after[0][2], "адрес всё ещё без значения")

    @scenario_test
    async def test_k3_legit_field_edit_from_list_saves_value(self, h) -> None:
        """K3: легитимный путь открытия и сохранения текстового поля."""
        await h.text("/start")
        draft = await _delivered_standard(h)
        active = _only_active(h, draft.id)
        address_id = _place_brick_id("адрес")
        before_address = [
            row for row in _value_rows(h, draft.id) if row[0] == address_id
        ]

        await _open_field_list(h, active.id)
        _assert_field_list(h)
        description_data = next(
            data
            for label, data in _field_buttons(h)
            if "Описание" in label
        )
        created_before = _created_count(h)
        await h.callback(description_data)

        self.assertEqual(
            _created_count(h), created_before + 1, "ровно один временный вопрос"
        )
        self.assertIn("Вопрос", h.screen_text())
        await h.text(NEW_VALUE)

        stored = _stored(h, draft.id)
        self.assertIn(NEW_VALUE, stored.publication_text)
        self.assertEqual(
            h.bot.content_text(h.chat_id, active.message_ids[0]),
            stored.publication_text,
        )
        self.assertIn(FIELDS_TITLE, _all_labels(h), "управление анонсом вернулось")

        values = _value_rows(h, draft.id)
        description_id = _place_brick_id("описание")
        description_rows = [row for row in values if row[0] == description_id]
        self.assertEqual(len(description_rows), 1)
        self.assertEqual(description_rows[0][1], "valid")
        self.assertIn(NEW_VALUE, description_rows[0][2] or "")
        # Адрес не тронут вводом текста.
        self.assertEqual(
            [row for row in values if row[0] == address_id],
            before_address,
            "ввод текста изменил значение адреса",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
