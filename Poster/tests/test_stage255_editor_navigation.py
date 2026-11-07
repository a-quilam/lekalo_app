# tests/test_stage255_editor_navigation.py
"""
Poster 2.5.5 §1+§3 — «Отменить» в вводе поля и контекстный «Назад» редактора.

Проверяется пользовательский контракт, а не устройство стека:

- клавиатура ввода заканчивается ОДНОЙ кнопкой «Отменить» в стиле `danger`;
  «Назад»/«Пропустить» больше нет, а у поля-даты нет и подсказки
  «⏳ Ожидаю …»;
- «Отменить» отбрасывает незавершённый ввод: значение и ревизия не меняются,
  сообщений не появляется, автор видит список полей;
- экран ошибки длины по-прежнему отменяется своим `anq_cancel` с ревизией —
  этот маршрут правкой не затронут;
- «Назад» из вложенных экранов редактора возвращает туда, откуда их
  открыли: доп. поля → управление анонсом, список полей → управление
  анонсом, значение доп. поля → список доп. полей;
- путь редактора принадлежит конкретному экземпляру: второй анонс в том же
  чате не наследует чужие экраны, пагинация списка путь не удлиняет, а его
  глубина не растёт при повторных заходах.

Тесты идут через настоящее приложение, FakeBot и временную SQLite
(`tests/integration_harness.py`): реальная `post_bot.db`, `.env` и сеть
Telegram не используются.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage255_editor_navigation -v
"""

from __future__ import annotations

import unittest

try:
    import announcement_controls as controls
    from announcement_store import AnnouncementFieldState
    from brick_types import BrickValueStatus
    from handlers import announcement_field_input as field_input
    from tests.integration_harness import HARNESS_IMPORT_ERROR, scenario_test
    from tests.test_stage27b_announcement_ui import (
        TemplateSpec,
        _choose_personal,
        _latest_draft,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )
    from tests.test_stage254_field_edit_inplace import (
        CONTENT,
        SPECS,
        _delivered,
        _field_buttons,
        _field_callback,
        _input_callbacks,
        _only_active,
        _open_input,
        _open_list,
        _stored,
    )
    from utils.inline_buttons import is_disabled

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


CANCEL = "Отменить"
EXTRAS_TITLE = "🧩 Дополнительные поля"


# --- помощники -------------------------------------------------------------


def _stack(h) -> list[tuple]:
    """Путь навигации в `user_data`: (экран, страница, кирпичик, экземпляр)."""
    ui = h.user_data().get("announcement_ui") or {}
    return [
        (
            entry.get("screen"),
            entry.get("page"),
            entry.get("brick_id"),
            entry.get("instance_id"),
        )
        for entry in ui.get("nav_stack") or []
    ]


def _screens(h) -> list[str]:
    return [entry[0] for entry in _stack(h)]


def _control_callbacks(h) -> list[str]:
    """Живые кнопки управления анонсом (`anp_`)."""
    return [
        data for _label, data in h.live_buttons() if data and data.startswith("anp_")
    ]


async def _open_extras(h, instance_id: int) -> None:
    """Управление анонсом → «🧩 Дополнительные поля»."""
    data = f"anp_extras:{int(instance_id)}"
    if data not in _control_callbacks(h):
        raise AssertionError(f"нет кнопки доп. полей: {h.live_buttons()}")
    await h.callback(data)


async def _nested_extra(h) -> str:
    """Открывает значение доп. поля и возвращает его `aw_exbackval`."""
    entries = [
        data
        for _label, data in h.screen_buttons()
        if data and data.startswith("aw_exedit:")
    ]
    if not entries:
        raise AssertionError(f"нет доп. полей на экране: {h.screen_buttons()}")
    await h.callback(entries[0])
    backs = [
        data
        for _label, data in h.screen_buttons()
        if data and data.startswith("aw_exbackval:")
    ]
    if not backs:
        raise AssertionError(f"не открылся экран значения: {h.screen_buttons()}")
    return backs[0]


async def _delivered_extras(h, *, photos: int = 0):
    """
    Личный шаблон с расширенной версией → доставленный анонс с доп. полями.

    `extended_content` делает `extras_available` истинным, поэтому кнопка
    «🧩 Дополнительные поля» есть в управлении экземпляром.
    """
    place_id, _alias = _seed_place(h)
    specs = list(SPECS) + [
        ("extra", "Стоимость", "short_text", {}),
    ]
    # Кандидат доп. поля есть только в расширенной версии: иначе кнопка
    # «🧩 Дополнительные поля» не показывается (`extras_available`).
    template_id, _revision, bricks = _seed_personal_template(
        h,
        name="Шаблон 2.5.5",
        specs=specs,
        content=CONTENT,
        extended_content=CONTENT + "\n[extra]",
    )
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    for _ in range(len(specs) + 6):
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
        if skip is not None:
            await h.callback(skip)
            continue
        await h.text("Значение")
    if photos:
        await h.photo("photo-0")
    else:
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


async def _delivered_paginated(h, *, fields: int = 7, photos: int = 0):
    """Личный шаблон с `fields` полями: в списке полей появляется пагинация."""
    place_id, _alias = _seed_place(h)
    specs: list[TemplateSpec] = [
        (f"p{index}", f"Поле {index}", "short_text", {}) for index in range(fields)
    ]
    content = "\n".join(f"[p{index}]" for index in range(fields))
    template_id, _revision, bricks = _seed_personal_template(
        h, name="Шаблон с пагинацией", specs=specs, content=content
    )
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    for _ in range(fields + 6):
        if any(
            data and data.startswith("aw_photo_none")
            for _label, data in h.screen_buttons()
        ):
            break
        await h.text("Значение")
    if photos:
        await h.photo("photo-0")
    else:
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


# --- клавиатура ввода ------------------------------------------------------


class InputKeyboardContractTests(unittest.TestCase):
    """§1: одна «Отменить» в стиле `danger`, у даты нет подсказки."""

    @staticmethod
    def _field(value_type: str, **rules) -> AnnouncementFieldState:
        return AnnouncementFieldState(
            brick_id=7,
            code="code",
            title="Поле",
            value_type=value_type,
            status=BrickValueStatus.MISSING,
            raw_value=None,
            rules=dict(rules),
        )

    def test_every_type_ends_with_single_cancel(self) -> None:
        for value_type in ("short_text", "long_text", "time", "date", "choice"):
            with self.subTest(value_type=value_type):
                keyboard = field_input.input_keyboard(self._field(value_type), 3)
                rows = keyboard.inline_keyboard
                self.assertEqual(len(rows[-1]), 1, f"{value_type}: строка навигации")
                button = rows[-1][0]
                self.assertEqual(button.text, CANCEL)
                self.assertEqual(button.callback_data, "anq_abort:3:7")
                self.assertEqual(button.style, "danger")

    def test_date_has_no_hint_row(self) -> None:
        keyboard = field_input.input_keyboard(self._field("date"), 3)
        rows = keyboard.inline_keyboard
        self.assertFalse(
            [button for row in rows for button in row if is_disabled(button)],
            "у даты не должно быть подсказки «⏳ Ожидаю …»",
        )
        callbacks = [
            str(button.callback_data)
            for row in rows
            for button in row
            if button.callback_data
        ]
        self.assertTrue(
            any(data.startswith("anq_day:") for data in callbacks), callbacks
        )

    def test_text_field_keeps_hint(self) -> None:
        keyboard = field_input.input_keyboard(self._field("short_text"), 3)
        rows = keyboard.inline_keyboard
        self.assertEqual(len(rows), 2, "подсказка и «Отменить»")
        self.assertTrue(is_disabled(rows[0][0]))
        self.assertEqual(rows[-1][0].text, CANCEL)

    def test_length_error_cancel_route_is_untouched(self) -> None:
        """Экран ошибки длины отменяется своим `anq_cancel` с ревизией."""
        keyboard = field_input.length_error_keyboard(5, 9, 42)
        rows = keyboard.inline_keyboard
        self.assertEqual(len(rows), 2)
        self.assertTrue(is_disabled(rows[0][0]))
        cancel = rows[1][0]
        self.assertEqual(cancel.text, CANCEL)
        self.assertEqual(str(cancel.callback_data), "anq_cancel:5:9:42")
        self.assertEqual(
            controls.parse_field_input_callback(str(cancel.callback_data)),
            ("cancel", 5, 9, ("42",)),
        )
        # У кнопки ввода хвоста нет: она собрана без ревизии.
        self.assertEqual(
            controls.field_input_callback(controls.FIELD_INPUT_ACTION_ABORT, 5, 9),
            "anq_abort:5:9",
        )


# --- «Отменить» ------------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CancelInputTests(unittest.TestCase):
    """§1: «Отменить» не пишет значение и возвращает список полей."""

    @scenario_test
    async def test_cancel_discards_input_without_messages(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)
        await _open_input(h, active.id, bricks["title"])

        before = _stored(h, draft.id)
        start = len(h.calls)
        await h.callback(_input_callbacks(h, "abort")[0])

        after = _stored(h, draft.id)
        self.assertEqual(after.revision, before.revision, "черновик изменён")
        self.assertEqual(
            after.publication_text, before.publication_text, "значение изменено"
        )
        self.assertTrue(_field_buttons(h), "список полей не вернулся")
        self.assertEqual(
            [call["type"] for call in h.calls[start:] if call["type"] == "message"],
            [],
            "«Отменить» отправила сообщение",
        )

        # Незавершённый ввод отброшен: следующий текст не попадает в поле.
        start = len(h.calls)
        await h.text("Текст после отмены")
        self.assertEqual(
            _stored(h, draft.id).publication_text,
            before.publication_text,
            "текст после отмены записался в поле",
        )
        self.assertTrue(_field_buttons(h), "список полей пропал")


# --- контекстный «Назад» ---------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class EditorNavigationTests(unittest.TestCase):
    """§3: вложенные экраны возвращают туда, откуда их открыли."""

    @scenario_test
    async def test_extras_nested_back_returns_to_editor(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered_extras(h)
        active = _only_active(h, draft.id)

        await _open_extras(h, active.id)
        self.assertEqual(_screens(h), ["editor", "extras"], _stack(h))

        back_value = await _nested_extra(h)
        self.assertEqual(_screens(h), ["editor", "extras", "extra_value"], _stack(h))

        await h.callback(back_value)
        self.assertEqual(_screens(h), ["editor", "extras"], _stack(h))

        back = [
            data
            for _label, data in h.screen_buttons()
            if data and data.startswith("aw_exback:")
        ]
        self.assertTrue(back, f"нет «Назад» на доп. полях: {h.screen_buttons()}")
        await h.callback(back[0])

        # Возврат — в управление анонсом, а не в «Текстовые поля».
        self.assertEqual(_screens(h), ["editor"], _stack(h))
        self.assertTrue(
            h.live_callbacks(f"anp_data:{active.id}"),
            f"управление анонсом не вернулось: {h.live_buttons()}",
        )

    @scenario_test
    async def test_field_input_back_returns_to_editor(self, h) -> None:
        await h.text("/start")
        draft, bricks = await _delivered(h, photos=1)
        active = _only_active(h, draft.id)

        await _open_list(h, active.id)
        self.assertEqual(_screens(h), ["editor", "fields"], _stack(h))

        await h.callback(_field_callback(h, bricks["title"]))
        await h.callback(_input_callbacks(h, "abort")[0])
        self.assertEqual(_screens(h), ["editor", "fields"], _stack(h))

        await h.callback(h.live_callbacks("anf_back:")[0])
        self.assertEqual(_screens(h), ["editor"], _stack(h))
        self.assertTrue(
            h.live_callbacks(f"anp_data:{active.id}"),
            "«Назад» со списка полей не вернул управление анонсу",
        )

    @scenario_test
    async def test_second_announcement_does_not_inherit_path(self, h) -> None:
        """Путь редактора принадлежит экземпляру, а не чату."""
        await h.text("/start")
        first, _bricks = await _delivered_extras(h)
        first_active = _only_active(h, first.id)
        await _open_extras(h, first_active.id)

        # Второй анонс в том же чате.
        second, bricks = await _delivered_extras(h)
        second_active = _only_active(h, second.id)
        self.assertNotEqual(first_active.id, second_active.id)

        await _open_list(h, second_active.id)
        self.assertEqual(_screens(h), ["editor", "fields"], _stack(h))
        self.assertTrue(
            all(entry[3] == second_active.id for entry in _stack(h)),
            f"в пути остался чужой экземпляр: {_stack(h)}",
        )

        await h.callback(h.live_callbacks("anf_back:")[0])
        self.assertEqual(_screens(h), ["editor"], _stack(h))
        self.assertTrue(
            h.live_callbacks(f"anp_data:{second_active.id}"),
            "вернулись к управлению другого анонса",
        )

    @scenario_test
    async def test_pagination_and_repeats_keep_path_short(self, h) -> None:
        """Пагинация не пишет путь, повторные заходы не растят глубину."""
        await h.text("/start")
        draft, _bricks = await _delivered_extras(h, photos=1)
        active = _only_active(h, draft.id)

        for _ in range(5):
            await _open_extras(h, active.id)
            self.assertLessEqual(len(_stack(h)), 8, _stack(h))
            back_value = await _nested_extra(h)
            self.assertLessEqual(len(_stack(h)), 8, _stack(h))
            await h.callback(back_value)
            await h.callback(
                next(
                    data
                    for _label, data in h.screen_buttons()
                    if data and data.startswith("aw_exback:")
                )
            )
            self.assertEqual(_screens(h), ["editor"], _stack(h))
            self.assertEqual(
                len(
                    [
                        data
                        for _label, data in h.live_buttons()
                        if data and data.startswith("anp_extras:")
                    ]
                ),
                1,
                "кнопки управления задвоились",
            )

        # Список полей с пагинацией: перерисовка страницы не удлиняет путь.
        paginated, _page_bricks = await _delivered_paginated(h)
        page_active = _only_active(h, paginated.id)
        await _open_list(h, page_active.id)
        depth = len(_stack(h))
        pages = h.live_callbacks("anf_page:")
        self.assertTrue(pages, f"нет пагинации в списке: {h.live_buttons()}")
        await h.callback(pages[0])
        self.assertEqual(len(_stack(h)), depth, "пагинация добавила шаг в путь")
        self.assertEqual(_screens(h), ["editor", "fields"], _stack(h))
        self.assertIsInstance(_stack(h)[-1][1], int, "страница не запомнена")

        await h.callback(h.live_callbacks("anf_back:")[0])
        self.assertEqual(_screens(h), ["editor"], _stack(h))


if __name__ == "__main__":
    unittest.main()
