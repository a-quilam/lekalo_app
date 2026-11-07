# tests/test_stage255_acceptance_a.py
"""
Poster 2.5.5, этап A — независимая приёмка пользовательских требований.

Модуль проверяет ТРЕБОВАНИЕ, а не устройство реализации. Все сценарии
идут через настоящее приложение, FakeBot и временную SQLite
(`tests/integration_harness.py`): рабочая `post_bot.db`, `.env` и сеть
Telegram не используются. Ожидания формулировок берутся из действующего
каталога `config/ui_texts.yaml` через `tests/ui_texts_expect.py` (yaml —
канон, `DEFAULT` не сверяется).

Что закреплено.

Блок 1 — правка поля созданного анонса:
- клавиатура ввода любого типа поля (текст/дата/время/выбор) заканчивается
  ровно одной «Отменить» в стиле `danger`; «Назад» и «Пропустить» в ней нет;
- у поля-даты нет неактивной подсказки «⏳ Ожидаю …», календарь на месте
  (дни и месяцы), ручной ввод даты сообщением применяется к анонсу;
- «Отменить» посреди незавершённого ввода возвращает к списку полей, не
  меняет ни ревизию, ни значения, не создаёт сообщений;
- поздняя «Отменить» после успешного сохранения значение НЕ откатывает;
- старый маршрут экрана ошибки длины цел: две кнопки (неактивная подсказка
  и `anq_cancel` с ревизией), отмена возвращает прежнее значение.

Блок 2 — контекстный «Назад»:
- из значения доп. поля — в доп. поля, не в текстовые поля;
- из вложенного экрана текстовых полей — в список полей, затем в редактор;
- страница 2 списка не теряется при возврате с вложенного экрана;
- путь второго анонса не наследует навигацию первого;
- устаревший/чужой callback получает короткий stale-ответ и не портит экран.

Блок 3 — пустые значения каталога:
- пустые строки каталога не оставляют дыр (`\\n\\n\\n`) ни на вопросе, ни
  на экране доп. полей;
- составной экран с пустым блоком сохраняет заголовок, описание и значение.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage255_acceptance_a -v
"""

from __future__ import annotations

import unittest

from sqlalchemy import text

try:
    from tests import ui_texts_expect as cat
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        TemplateSpec,
        _choose_personal,
        _latest_draft,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )
    from tests.test_stage254_field_edit_inplace import (
        SPECS,
        _delivered,
        _field_buttons,
        _field_callback,
        _input_callbacks,
        _input_markup,
        _only_active,
        _open_list,
        _stored,
    )
    from tests.test_stage254_long_text_cancel import (
        LONG_NO_PHOTO,
        OLD_DESCRIPTION,
        _error_keyboard,
        _open_description,
        _value_rows,
    )
    from tests.test_stage254_long_text_cancel import (
        _delivered as _delivered_two_fields,
    )
    from tests.test_stage255_editor_navigation import (
        CONTENT,
        _delivered_extras,
        _delivered_paginated,
        _nested_extra,
        _open_extras,
        _screens,
        _stack,
    )
    from utils.inline_buttons import is_disabled
    from utils.parsers import parse_date_flexible

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


CANCEL = "Отменить"
EXTRAS_TITLE_KEY = "wizard.extras.title"
INPUT_HINT_FRAGMENT_KEY = "wizard.field_input.hint"


# --- помощники -------------------------------------------------------------


def _message_id(h, draft_id: int) -> int:
    """Сообщение анонса с клавиатурой управления (0–1 фото)."""
    return int(_only_active(h, draft_id).message_ids[0])


def _input_rows(h, message_id: int) -> list[list]:
    """Строки фактической клавиатуры ввода на сообщении анонса."""
    markup = _input_markup(h, message_id)
    if markup is None or not hasattr(markup, "inline_keyboard"):
        raise AssertionError("клавиатура ввода не показана")
    return list(markup.inline_keyboard or [])


def _flat_rows(rows: list[list]) -> list:
    return [button for row in rows for button in row]


def _values(h, draft_id: int) -> dict[int, tuple[str, object]]:
    """Значения всех полей черновика: brick_id -> (state, value_json)."""
    with h.session_factory() as session:
        rows = session.execute(
            text(
                "SELECT brick_id, state, value_json FROM announcement_values "
                "WHERE draft_id = :draft_id ORDER BY brick_id"
            ),
            {"draft_id": int(draft_id)},
        ).all()
    return {int(row[0]): (str(row[1]), row[2]) for row in rows}


def _created(h, start: int, kind: str = "message") -> list[dict]:
    """Вызовы данного вида, создающие НОВОЕ сообщение, после отметки `start`."""
    create_types = {"message", "photo", "media_group", "rich_message"}
    if kind == "any_create":
        return [call for call in h.calls[start:] if call["type"] in create_types]
    return [call for call in h.calls[start:] if call["type"] == kind]


async def _open_input(h, instance_id: int, code: str, bricks) -> int:
    """Открывает ввод поля `code` и возвращает message_id анонса."""
    draft_id = _only_active(h, instance_id).draft_id
    message_id = _message_id(h, draft_id)
    if not _field_buttons(h):
        await _open_list(h, instance_id)
    await h.callback(_field_callback(h, bricks[code]))
    return message_id


async def _delivered_extras_pages(h, *, extras: int = 6):
    """
    Доставленный анонс с `extras` дополнительными полями.

    Расширенная версия шаблона делает `extras_available` истинным, поэтому в
    управлении появляется «🧩 Дополнительные поля», а список доп. полей
    разбивается на две страницы (5 на страницу).
    """
    place_id, _alias = _seed_place(h)
    specs = list(SPECS) + [
        (f"x{index}", f"Доп {index}", "short_text", {}) for index in range(extras)
    ]
    extended = CONTENT + "".join(f"\n[x{index}]" for index in range(extras))
    template_id, _revision, bricks = _seed_personal_template(
        h,
        name="Шаблон с доп. полями",
        specs=specs,
        content=CONTENT,
        extended_content=extended,
    )
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    for _ in range(len(specs) + 8):
        if any(
            data and data.startswith("aw_photo_none")
            for _label, data in h.screen_buttons()
        ):
            break
        buttons = [data for _label, data in h.screen_buttons()]
        preset = next((data for data in buttons if data.startswith("aw_timeset")), None)
        if preset is not None:
            await h.callback(preset)
            continue
        choice = next((data for data in buttons if data.startswith("aw_choice:")), None)
        if choice is not None:
            await h.callback(choice)
            continue
        skip = next((data for data in buttons if data.startswith("aw_qskip")), None)
        if skip is not None and any(data.startswith("aw_calday") for data in buttons):
            await h.callback(skip)
            continue
        await h.text("Значение")
    await h.callback(
        next(
            data
            for _label, data in h.screen_buttons()
            if data and data.startswith("aw_photo_none")
        )
    )
    draft = _latest_draft(h)
    if _stored(h, draft.id).publication_status != "sent":
        raise AssertionError("анонс не доставлен")
    return draft, bricks


def _screen_callbacks(h, prefix: str) -> list[str]:
    return [
        data for _label, data in h.screen_buttons() if data and data.startswith(prefix)
    ]


# --- Блок 1: клавиатура ввода ---------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class InputKeyboardRequirementTests(unittest.TestCase):
    """§1 (требования 1–2): ввод без «Назад»/«Пропустить», дата без подсказки."""

    @scenario_test
    async def test_1_all_types_end_with_single_danger_cancel(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)

        cancel_label = cat.text("wizard.field_input.cancel")
        back_label = cat.text("buttons.nav.back")
        skip_label = cat.text("buttons.wizard.skip")

        for code in ("title", "clock", "day", "pick"):
            with self.subTest(field=code):
                if not _field_buttons(h):
                    await _open_list(h, active.id)
                await h.callback(_field_callback(h, bricks[code]))
                rows = _input_rows(h, message_id)
                buttons = _flat_rows(rows)

                # «Назад» и «Пропустить» отсутствуют и подписью, и callback'ом.
                for button in buttons:
                    self.assertNotEqual(button.text, back_label, code)
                    self.assertNotEqual(button.text, skip_label, code)
                    data = str(button.callback_data or "")
                    self.assertFalse(
                        data.startswith("anq_back:"), f"{code}: «Назад» в вводе"
                    )
                    self.assertFalse(
                        data.startswith("anq_skip:"), f"{code}: «Пропустить» в вводе"
                    )

                # Ровно одна «Отменить» в стиле `danger` в ПОСЛЕДНЕЙ строке.
                cancels = [b for b in buttons if b.text == cancel_label]
                self.assertEqual(len(cancels), 1, f"{code}: {buttons}")
                self.assertEqual(len(rows[-1]), 1, f"{code}: хвост клавиатуры")
                tail = rows[-1][0]
                self.assertEqual(tail.text, cancel_label, code)
                self.assertEqual(tail.style, "danger", code)
                self.assertFalse(is_disabled(tail), code)
                self.assertTrue(
                    str(tail.callback_data or "").startswith("anq_abort:"),
                    f"{code}: {tail.callback_data}",
                )
                await h.callback(_input_callbacks(h, "abort")[0])

    @scenario_test
    async def test_1b_long_text_input_uses_same_cancel_tail(self, h) -> None:
        """Длинный текст — тот же хвост клавиатуры: одна «Отменить»."""
        await h.text("/start")
        draft, bricks = await _delivered_two_fields(h, photos=0)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)

        await _open_description(h, active.id)
        rows = _input_rows(h, message_id)
        buttons = _flat_rows(rows)
        cancel_label = cat.text("wizard.field_input.cancel")

        self.assertEqual(len(rows[-1]), 1, rows[-1])
        self.assertEqual(rows[-1][0].text, cancel_label)
        self.assertEqual(rows[-1][0].style, "danger")
        self.assertTrue(str(rows[-1][0].callback_data).startswith("anq_abort:"))
        self.assertFalse(
            [
                b
                for b in buttons
                if b.text
                in (cat.text("buttons.nav.back"), cat.text("buttons.wizard.skip"))
            ],
            f"«Назад»/«Пропустить» в вводе длинного текста: {[b.text for b in buttons]}",
        )
        self.assertFalse(h.live_callbacks("anq_back:"), "маршрут «Назад» в вводе")
        self.assertFalse(h.live_callbacks("anq_skip:"), "маршрут «Пропустить» в вводе")

    @scenario_test
    async def test_2b_invalid_date_text_keeps_value_and_input(self, h) -> None:
        """Неверный текст даты: значение прежнее, ввод не «схлопывается»."""
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)
        await _open_input(h, active.id, "day", bricks)
        before_values = _values(h, draft.id)
        before_publication = _stored(h, draft.id).publication_text

        await h.text("32.32.2030")

        self.assertEqual(
            _values(h, draft.id), before_values, "неверная дата записалась в поле"
        )
        self.assertEqual(
            _stored(h, draft.id).publication_text,
            before_publication,
            "анонс изменился после неверной даты",
        )
        # Ввод остаётся тем же: автор исправляет дату, не открывая поле заново.
        self.assertTrue(
            any(
                str(b.callback_data or "").startswith("anq_day:")
                for b in _flat_rows(_input_rows(h, message_id))
            ),
            f"клавиатура ввода даты потеряна: {_input_rows(h, message_id)}",
        )
        self.assertFalse(_field_buttons(h), "ввод даты закрылся на ошибке")

    @scenario_test
    async def test_2_date_has_no_hint_but_calendar_and_text_input(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)
        hint = cat.fragment(INPUT_HINT_FRAGMENT_KEY)

        await _open_input(h, active.id, "day", bricks)
        rows = _input_rows(h, message_id)
        buttons = _flat_rows(rows)

        # Никакой неактивной «⏳ Ожидаю …» на клавиатуре даты.
        self.assertFalse(
            [b for b in buttons if hint and b.text.startswith(hint)],
            f"у даты осталась подсказка: {[b.text for b in buttons]}",
        )
        self.assertFalse(
            [b for b in buttons if is_disabled(b)],
            f"у даты не должно быть неактивных кнопок: {[b.text for b in buttons]}",
        )
        # Календарь: дни и месяцы.
        self.assertTrue(
            any(str(b.callback_data or "").startswith("anq_day:") for b in buttons),
            f"нет кнопок дней: {[b.callback_data for b in buttons]}",
        )
        await h.callback(
            next(
                str(b.callback_data)
                for b in buttons
                if str(b.callback_data or "").startswith("anq_cal:")
            )
        )
        month_buttons = _flat_rows(_input_rows(h, message_id))
        self.assertTrue(
            any(
                str(b.callback_data or "").startswith("anq_month:")
                for b in month_buttons
            ),
            f"нет кнопок месяцев: {[b.callback_data for b in month_buttons]}",
        )
        await h.callback(
            next(
                str(b.callback_data)
                for b in month_buttons
                if str(b.callback_data or "").startswith("anq_calquick:")
            )
        )
        self.assertTrue(
            any(
                str(b.callback_data or "").startswith("anq_day:")
                for b in _flat_rows(_input_rows(h, message_id))
            ),
            "не вернулись к дням календаря",
        )

        # Ручной ввод даты сообщением применяется к анонсу.
        await h.callback(_input_callbacks(h, "abort")[0])
        if not _field_buttons(h):
            await _open_list(h, active.id)
        await h.callback(_field_callback(h, bricks["day"]))
        expected = parse_date_flexible("через неделю")
        self.assertIsNotNone(expected, "парсер дат не разобрал «через неделю»")
        await h.text("через неделю")

        stored = _stored(h, draft.id)
        self.assertIn(expected, stored.publication_text or "", "дата не применена")
        labels = [label for label, _data in _field_buttons(h)]
        self.assertTrue(
            any("Дата — заполнено" in label for label in labels),
            f"список полей не показывает дату: {labels}",
        )
        self.assertIsNone(h.screen_message_id(), "появился служебный экран")


# --- Блок 1: «Отменить» ----------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CancelInputRequirementTests(unittest.TestCase):
    """§1 (требования 3–4): отмена ввода ничего не пишет и не откатывает."""

    @scenario_test
    async def test_3_cancel_mid_input_returns_editor_without_messages(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)
        await _open_input(h, active.id, "title", bricks)

        before_values = _values(h, draft.id)
        before_revision = _stored(h, draft.id).revision
        before_publication = _stored(h, draft.id).publication_text
        start = len(h.calls)
        replies_before = len(h.replies())

        abort_data = _input_callbacks(h, "abort")[0]
        await h.callback(abort_data)

        after = _stored(h, draft.id)
        self.assertEqual(
            _values(h, draft.id), before_values, "отмена изменила значения полей"
        )
        self.assertEqual(after.revision, before_revision, "отмена записала в БД")
        self.assertEqual(after.publication_text, before_publication)
        self.assertEqual(
            _created(h, start), [], "«Отменить» создала текстовое сообщение"
        )
        self.assertEqual(len(h.replies()), replies_before, "появился ответ в чате")
        # Вернулись к списку полей на том же сообщении анонса.
        self.assertTrue(
            _field_buttons(h), f"список полей не вернулся: {h.live_buttons()}"
        )
        self.assertEqual(_input_callbacks(h, ""), [], "клавиатура ввода осталась")
        self.assertEqual(_only_active(h, draft.id).message_ids, (message_id,))
        self.assertIsNone(h.screen_message_id(), "появился служебный экран")

        # Ввод действительно прекращён: следующий текст не попадает в поле.
        await h.text("Текст после отмены")
        self.assertEqual(
            _values(h, draft.id), before_values, "текст после отмены записался в поле"
        )
        self.assertEqual(
            _stored(h, draft.id).publication_text,
            before_publication,
            "анонс переиздан после отмены",
        )
        self.assertTrue(_field_buttons(h), "список полей пропал после текста")

        # Повторное нажатие той же «Отменить» безопасно и без сообщений.
        start = len(h.calls)
        await h.callback(abort_data)
        self.assertEqual(
            _values(h, draft.id), before_values, "повторная отмена изменила значения"
        )
        self.assertEqual(
            _created(h, start), [], "повторная «Отменить» создала сообщение"
        )
        self.assertTrue(_field_buttons(h), "повторная отмена сломала список полей")

    @scenario_test
    async def test_4_late_cancel_does_not_rollback_saved_value(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        await _open_input(h, active.id, "title", bricks)
        stale_abort = _input_callbacks(h, "abort")[0]

        await h.text("Новое название после сохранения")
        saved = _stored(h, draft.id)
        self.assertIn(
            "Новое название после сохранения",
            saved.publication_text or "",
            "значение не сохранилось",
        )
        saved_values = _values(h, draft.id)

        # Поздний callback кнопки, которой уже нет на экране.
        start = len(h.calls)
        await h.callback(stale_abort)

        after = _stored(h, draft.id)
        self.assertEqual(
            after.publication_text,
            saved.publication_text,
            "поздняя «Отменить» откатила сохранённое значение",
        )
        self.assertEqual(
            after.revision, saved.revision, "поздняя «Отменить» писала в БД"
        )
        self.assertEqual(_values(h, draft.id), saved_values, "значения изменились")
        # Экран не сломан: снова список полей того же анонса.
        self.assertTrue(
            _field_buttons(h), f"список полей не вернулся: {h.live_buttons()}"
        )
        self.assertEqual(h.screen_text(), "", "появился служебный экран")
        self.assertEqual(_created(h, start), [], "поздняя «Отменить» создала сообщение")


# --- Блок 1: старый маршрут экрана ошибки длины ----------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class LengthErrorRouteRequirementTests(unittest.TestCase):
    """§1 (требование 5): экран ошибки длины не сломан правкой."""

    @scenario_test
    async def test_5_length_screen_and_cancel_restore_previous_value(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered_two_fields(h, photos=0)
        active = _only_active(h, draft.id)
        message_id = _message_id(h, draft.id)
        published = _stored(h, draft.id).publication_text
        second_before = next(
            row for row in _value_rows(h, draft.id) if row[0] == bricks["f1"]
        )

        await _open_description(h, active.id)
        await h.text(LONG_NO_PHOTO)

        rows = _error_keyboard(h, message_id)
        self.assertEqual(len(rows), 2, f"на экране ошибки не две строки: {rows}")
        hint = rows[0][0]
        self.assertTrue(is_disabled(hint), "подсказка должна быть неактивной")
        self.assertEqual(hint.text, cat.text("wizard.length_refusal.edit_hint"))
        self.assertIsNone(hint.callback_data, "у подсказки не должно быть callback")
        cancel = rows[1][0]
        self.assertEqual(len(rows[1]), 1, rows[1])
        self.assertEqual(cancel.text, cat.text("wizard.length_refusal.edit_cancel"))
        cancel_data = str(cancel.callback_data or "")
        self.assertTrue(cancel_data.startswith("anq_cancel:"), cancel_data)
        tail = cancel_data.split(":")[3:]
        self.assertEqual(len(tail), 1, f"нет ревизии в callback: {cancel_data}")
        self.assertTrue(tail[0].isdigit(), cancel_data)
        self.assertEqual(
            int(tail[0]),
            _stored(h, draft.id).revision,
            "ревизия в кнопке не совпадает с черновиком",
        )

        # Отмена возвращает прежнее значение поля и не трогает другие.
        await h.callback(cancel_data)
        restored = next(
            row for row in _value_rows(h, draft.id) if row[0] == bricks["f0"]
        )
        self.assertEqual(restored[1], "valid")
        self.assertIn(OLD_DESCRIPTION, restored[2] or "")
        self.assertNotIn(
            LONG_NO_PHOTO, restored[2] or "", "незавершённая правка осталась"
        )
        self.assertEqual(
            next(row for row in _value_rows(h, draft.id) if row[0] == bricks["f1"]),
            second_before,
            "отмена откатила другое поле",
        )
        after = _stored(h, draft.id)
        self.assertEqual(after.publication_text, published, "анонс переиздан")
        self.assertEqual(after.delivered_message_ids, (message_id,))
        self.assertTrue(_field_buttons(h), "список полей не вернулся")
        self.assertEqual(h.live_callbacks("anq_cancel"), [], "экран ошибки остался")


# --- Блок 2: контекстный «Назад» -------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ContextualBackRequirementTests(unittest.TestCase):
    """§3 (требования 6–9): возврат идёт по фактическому пути."""

    @scenario_test
    async def test_6_extras_value_back_returns_to_extras(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered_extras(h)
        active = _only_active(h, draft.id)
        await _open_extras(h, active.id)
        back_value = await _nested_extra(h)
        self.assertEqual(_screens(h), ["editor", "extras", "extra_value"], _stack(h))

        await h.callback(back_value)

        self.assertEqual(_screens(h), ["editor", "extras"], _stack(h))
        self.assertTrue(
            h.screen_text().startswith(cat.text(EXTRAS_TITLE_KEY)),
            f"вернулись не в доп. поля: {h.screen_text()[:120]!r}",
        )
        self.assertTrue(
            _screen_callbacks(h, "aw_exback:"),
            f"нет «Назад» на доп. полях: {h.screen_buttons()}",
        )
        self.assertFalse(
            h.live_callbacks("anf_sel:"),
            "показан список текстовых полей вместо доп. полей",
        )

    @scenario_test
    async def test_7_fields_nested_back_chain_to_editor(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        await _open_list(h, active.id)
        self.assertEqual(_screens(h), ["editor", "fields"], _stack(h))
        await h.callback(_field_callback(h, bricks["title"]))

        # Вложенный экран ввода: «Назад» старой клавиатуры возвращает список.
        back = f"anq_back:{active.id}:{bricks['title']}"
        await h.callback(back)
        self.assertEqual(_screens(h), ["editor", "fields"], _stack(h))
        self.assertTrue(_field_buttons(h), "список полей не вернулся")
        self.assertEqual(_input_callbacks(h, ""), [], "клавиатура ввода осталась")

        # «Назад» списка — в общий редактор (управление анонсом).
        await h.callback(h.live_callbacks("anf_back:")[0])
        self.assertEqual(_screens(h), ["editor"], _stack(h))
        self.assertTrue(
            h.live_callbacks(f"anp_data:{active.id}"),
            f"управление анонсом не вернулось: {h.live_buttons()}",
        )
        self.assertFalse(h.live_callbacks("anf_sel:"), "список полей остался")

    @scenario_test
    async def test_8_page_two_survives_return_from_nested(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered_paginated(h)
        active = _only_active(h, draft.id)
        await _open_list(h, active.id)
        pages = h.live_callbacks("anf_page:")
        self.assertTrue(pages, f"нет пагинации: {h.live_buttons()}")
        await h.callback(pages[0])
        page_two_items = [
            label
            for label, _data in _field_buttons(h)
            if label.startswith(("6.", "7."))
        ]
        self.assertTrue(page_two_items, f"страница 2 не открылась: {_field_buttons(h)}")

        # Вложенный ввод открыт со страницы 2; «Отменить» и старая «Назад»
        # обе обязаны вернуть ту же страницу 2, а не первую.
        selected = _field_buttons(h)[0][1]
        _, instance_id, brick_id = selected.split(":")

        await h.callback(selected)
        await h.callback(_input_callbacks(h, "abort")[0])
        labels = [label for label, _data in _field_buttons(h)]
        self.assertTrue(
            any("6. Поле 5" in label for label in labels),
            f"«Отменить» вернула не на страницу 2: {labels}",
        )
        self.assertFalse(
            any("1. Поле 0" in label for label in labels),
            f"«Отменить» показала первую страницу: {labels}",
        )

        await h.callback(_field_buttons(h)[0][1])
        await h.callback(f"anq_back:{instance_id}:{brick_id}")

        labels = [label for label, _data in _field_buttons(h)]
        self.assertTrue(
            any("6. Поле 5" in label for label in labels),
            f"вернулись не на страницу 2: {labels}",
        )
        self.assertFalse(
            any("1. Поле 0" in label for label in labels),
            f"показана первая страница: {labels}",
        )
        self.assertTrue(
            any(label == "2/2" for label, _data in h.live_buttons()),
            f"индикатор страницы не 2/2: {h.live_buttons()}",
        )

    @scenario_test
    async def test_8b_extras_page_two_survives_return_from_value(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered_extras_pages(h)
        active = _only_active(h, draft.id)
        await _open_extras(h, active.id)
        pages = _screen_callbacks(h, "aw_exbpage:")
        self.assertTrue(pages, f"нет пагинации доп. полей: {h.screen_buttons()}")
        await h.callback(pages[0])
        page_two_text = h.screen_text()
        self.assertIn("6. Доп 5", page_two_text)

        edits = _screen_callbacks(h, "aw_exedit:")
        self.assertTrue(edits, f"нет полей на странице 2: {h.screen_buttons()}")
        await h.callback(edits[0])
        self.assertEqual(_screens(h), ["editor", "extras", "extra_value"], _stack(h))
        await h.callback(_screen_callbacks(h, "aw_exbackval:")[0])

        self.assertEqual(_screens(h), ["editor", "extras"], _stack(h))
        self.assertIn(
            "6. Доп 5",
            h.screen_text(),
            f"вернулись не на страницу 2 доп. полей: {h.screen_text()[:160]!r}",
        )

    @scenario_test
    async def test_9_second_announcement_does_not_inherit_path(self, h) -> None:
        await h.text("/start")
        first, _bricks = await _delivered_extras(h)
        first_active = _only_active(h, first.id)
        await _open_extras(h, first_active.id)
        self.assertEqual(_screens(h), ["editor", "extras"], _stack(h))

        second, _second_bricks = await _delivered_extras(h)
        second_active = _only_active(h, second.id)
        self.assertNotEqual(first_active.id, second_active.id)

        await _open_extras(h, second_active.id)
        back_value = await _nested_extra(h)
        await h.callback(back_value)
        await h.callback(_screen_callbacks(h, "aw_exback:")[0])

        self.assertEqual(_screens(h), ["editor"], _stack(h))
        self.assertTrue(
            all(entry[3] == second_active.id for entry in _stack(h)),
            f"в пути остался чужой анонс: {_stack(h)}",
        )
        self.assertTrue(
            h.live_callbacks(f"anp_data:{second_active.id}"),
            f"управление вторым анонсом не вернулось: {h.live_buttons()}",
        )

    @scenario_test
    async def test_10_stale_callbacks_answer_and_keep_screen(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered_extras(h)
        active = _only_active(h, draft.id)
        await _open_extras(h, active.id)
        before_buttons = h.live_buttons()
        before_text = h.screen_text()
        before_draft = _stored(h, draft.id)
        start = len(h.calls)

        # Чужой/несуществующий экземпляр и инстанс из другого чата.
        await h.callback("anf_sel:999:12")
        await h.callback("anq_abort:999:12")
        await h.callback(f"anp_data:{active.id}", chat_id=h.chat_id + 1)

        answers = h.answer_texts()
        self.assertGreaterEqual(len(answers), 3, f"нет ответов: {answers}")
        for answer in answers[-3:]:
            self.assertTrue(
                cat.is_stale(answer),
                f"нет объяснения устаревшей кнопки: {answer!r}",
            )
        self.assertEqual(_created(h, start), [], "устаревшая кнопка создала сообщение")
        self.assertEqual(h.live_buttons(), before_buttons, "клавиатуры изменились")
        self.assertEqual(h.screen_text(), before_text, "экран испорчен")
        after_draft = _stored(h, draft.id)
        self.assertEqual(after_draft.publication_text, before_draft.publication_text)
        self.assertEqual(after_draft.revision, before_draft.revision)


# --- Блок 3: пустые значения каталога --------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class EmptyCatalogScreenRequirementTests(unittest.TestCase):
    """§3 (требования 11–12): пустой блок не оставляет дыры на экране."""

    def test_11_catalog_premise_is_empty_keys(self) -> None:
        for key in (
            "wizard.question.optional",
            "wizard.question.missing",
            "wizard.extras.intro",
        ):
            with self.subTest(key=key):
                self.assertTrue(cat.is_empty_block(key), f"{key} не пуст в каталоге")

    @scenario_test
    async def test_11_live_screens_have_no_empty_separators(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered_extras_pages(h, extras=2)
        active = _only_active(h, draft.id)

        # Экран вопроса: пустые optional/missing не оставляют дыр.
        await _open_list(h, active.id)
        for label, _data in _field_buttons(h):
            self.assertNotIn("\n\n\n", label, "пустая строка в подписи кнопки")
        await h.callback(_field_buttons(h)[0][1])
        # Ввод поля — та же клавиатура анонса, текст не меняется.
        self.assertNotIn("\n\n\n", h.screen_text())
        await h.callback(_input_callbacks(h, "abort")[0])
        # Возврат в редактор: дальше открываем доп. поля из управления.
        await h.callback(h.live_callbacks("anf_back:")[0])

        # Экран доп. полей (вступление пустое).
        await _open_extras(h, active.id)
        extras_text = h.screen_text()
        self.assertNotIn("\n\n\n", extras_text, "пустой блок оставил дыру")
        self.assertEqual(
            extras_text, extras_text.strip("\n"), "лишние переводы строк по краям"
        )
        parts = extras_text.split("\n\n")
        self.assertTrue(parts and parts[0].strip(), f"экран пуст: {extras_text!r}")
        self.assertTrue(
            parts[0].startswith(cat.text(EXTRAS_TITLE_KEY)),
            f"экран не начинается с заголовка: {parts[0]!r}",
        )
        for block in parts:
            for line in block.split("\n"):
                self.assertTrue(
                    line.strip(), f"пустая строка-разделитель: {extras_text!r}"
                )

    @scenario_test
    async def test_11b_question_screen_with_empty_blocks(self, h) -> None:
        """Экран вопроса с пустыми optional/missing: ни дыры, ни потери блоков."""
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        specs: list[TemplateSpec] = [("q1", "Название", "short_text", {})]
        template_id, _revision, _bricks = _seed_personal_template(
            h, name="Шаблон вопроса", specs=specs, content="[q1]"
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)

        question = h.screen_text()
        self.assertNotIn("\n\n\n", question, "пустой блок оставил дыру")
        cat.assert_no_empty_block(self, question, "wizard.question.optional")
        cat.assert_no_empty_block(self, question, "wizard.question.missing")
        self.assertEqual(
            question.split("\n\n"),
            [
                cat.text("wizard.question.header", step=1, total=1, title="Название"),
                cat.text("wizard.hint.short_text"),
            ],
            "пустые блоки оставили лишние строки на живой экране",
        )

    @scenario_test
    async def test_12_composite_screen_keeps_header_description_value(self, h) -> None:
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        specs: list[TemplateSpec] = [
            ("d1", "Название", "short_text", {}),
            ("d2", "Формат", "short_text", {}),
        ]
        template_id, _revision, bricks = _seed_personal_template(
            h, name="Шаблон с описанием", specs=specs, content="[d1]\n[d2]"
        )
        with h.session_factory() as session:
            session.execute(
                text("UPDATE bricks SET description = :d WHERE id = :i"),
                {"d": "Инструкция автору", "i": int(bricks["d1"])},
            )
            session.commit()
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)

        question = h.screen_text()
        cat.assert_shows(self, question, "wizard.question.header", step=1, total=2)
        cat.assert_shows(
            self,
            question,
            "wizard.question.description_html",
            description="Инструкция автору",
        )
        cat.assert_shows(self, question, "wizard.hint.short_text")
        self.assertNotIn("\n\n\n", question, "пустой блок оставил дыру")

        # Отвечаем и возвращаемся: заголовок, описание и значение — все видны.
        await h.text("Первое значение")
        back = h.screen_buttons()
        await h.callback(
            next(data for _label, data in back if data and data.startswith("aw_qback:"))
        )
        question = h.screen_text()
        cat.assert_shows(self, question, "wizard.question.header", step=1, total=2)
        cat.assert_shows(
            self,
            question,
            "wizard.question.description_html",
            description="Инструкция автору",
        )
        cat.assert_shows(
            self, question, "wizard.question.current_value", value="Первое значение"
        )
        self.assertNotIn("\n\n\n", question, "пустой блок оставил дыру")
        cat.assert_no_empty_block(self, question, "wizard.question.optional")


if __name__ == "__main__":
    unittest.main()
