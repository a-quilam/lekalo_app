# tests/test_stage261_c_templates.py
"""
Poster 2.6.1, этап C — экраны шаблона (§6 и §7 задания владельца).

Проверяются ТРЕБОВАНИЯ ВЛАДЕЛЬЦА (`docs/agent_team/task_261_20261110.md`
§6–§7), а не устройство реализации. Все сценарии идут через настоящее
приложение (`bot.build_application`), `Application.process_update`, FakeBot
и временную SQLite (`tests/integration_harness.py`): рабочая `post_bot.db`,
`.env` и сеть Telegram не используются.

Что закрепляется.

§6.2 — карточка стандарта при ВЫКЛЮЧЕННОМ расширенном варианте: шапка
`📄 Шаблон — {площадка}`, жирный подзаголовок «Простой шаблон» и текст
простого шаблона; расширенного текста, строк «Тип:» и «Версия:» нет.
Кнопки строго по строкам: `[✏️ Правка шаблона] [➕ Включить расширенный
шаблон]` / `[🧩 Кирпичики]` / `[♻️ Восстановить стандарт]` / `[◀️ К месту]`;
кнопки «Условные части» нет.

§6.3 — карточка при ВКЛЮЧЁННОМ варианте: добавляется строка «Расширенный
шаблон включён.», сам расширенный текст в карточке не дублируется; кнопки
`[✏️ Простой шаблон] [✏️ Расширенный шаблон]` / `[🧩 Условные части]` /
`[🧩 Кирпичики]` / `[♻️ Восстановить стандарт] [Выключить расширенный
шаблон]` / `[◀️ К месту]`.

§6.4 — переключение одним нажатием: правится ТЕКУЩАЯ карточка и её
клавиатура, отдельного сообщения «включено/выключено» нет, повторы
идемпотентны, оба текста и условные части сохраняются.

§7.2–7.3 — экран правки: заголовок по варианту, «Текущий текст:», сохранённый
текст с переносами, короткий абзац-инструкция; кнопки
`[📋 Список кирпичиков] [❔ Как редактировать]` / `[◀️ К шаблону]`; кнопки и
сценария «Очистить» нет (устаревший `tp_clear` получает короткий STALE).

§7.4 — «Список кирпичиков»: `[код] — название` и краткое описание, «◀️ Назад»
возвращает в редактор ТОГО ЖЕ варианта.

§7.5 — «Как редактировать»: короткий текст, пример отдельным блоком, строка
форматирования; внизу только `[◀️ К редактору]` — и он возвращает в
редактор того же варианта.

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage261_c_templates -v
"""

from __future__ import annotations

import unittest

from tests import ui_texts_expect as cat

try:
    from brick_store import create_personal_brick
    from template_store import (
        create_personal_template,
        get_standard_template,
        get_template_version,
    )
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.test_stage26_a2_extended_toggle import (
        _callbacks,
        _extended_text,
        _flag,
        _labels,
        _message_calls,
        _open_card,
        _rules,
        _seed_place,
    )
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


# --- формулировки контракта (§6.2/§6.3/§7.2/§7.3/§7.5) ---------------------

CARD_TITLE_KEY = "templates.card.title"
SIMPLE_TITLE = cat.text("templates.card.simple_html")
EXTENDED_ON = cat.text("templates.card.extended_on")

EDIT_TITLE = cat.text("templates.edit.title")
EDIT_TITLE_EXT = cat.text("templates.edit.title_extended")
EDIT_CURRENT = cat.text("templates.edit.current")
EDIT_INSTRUCTION = cat.text("templates.edit.instruction")

BTN_EDIT = cat.text("buttons.templates.tekst")
BTN_SIMPLE = cat.text("buttons.templates.prostoy_shablon")
BTN_EXT = cat.text("buttons.templates.rasshirennyy_shablon")
BTN_ON = cat.text("buttons.templates.vklyuchit_dopolnitelnyy_shablon")
BTN_OFF = cat.text("buttons.templates.vyklyuchit_dopolnitelnyy_shablon")
BTN_COND = cat.text("buttons.templates.uslovnye_chasti")
BTN_BRICKS = cat.text("buttons.templates.kirpichiki")
BTN_RESET = cat.text("buttons.templates.vosstanovit_standart")
BTN_LIST = cat.text("buttons.templates.spisok_kirpichikov")
BTN_HELP = cat.text("buttons.templates.kak_redaktirovat")
BTN_TO_EDITOR = cat.text("buttons.templates.k_redaktoru")
BTN_TO_TEMPLATE = cat.text("buttons.templates.k_shablonu")
BTN_TO_PLACE = cat.text("admin.proposal_admin_list.k_mestu")

# Фрагменты, которые есть только в расширенном тексте стандарта (§1.2).
EXTENDED_ONLY = ("[условие_входа]", "[куратор]", "[подвал]")
# Фрагменты простого текста стандарта (§1.1).
SIMPLE_ONLY = ("Мероприятие ведёт:", "[описание]")

STALE_TEMPLATES = cat.text("errors.stale.templates")


def _rows(h) -> list[list[tuple[str, str | None]]]:
    """Клавиатура экрана как строки кнопок — так её видит автор."""
    record = h.screen()
    assert record is not None, "экрана ещё не было"
    markup = record.get("reply_markup")
    assert markup is not None, "у экрана нет клавиатуры"
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


async def _open_personal_card(h, template_id: int) -> None:
    """Открыть карточку личного шаблона из списка «Мои шаблоны»."""
    await h.callback("manage_templates")
    await h.callback(f"tp_open:{template_id}")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class OffCardTests(unittest.TestCase):
    """§6.2: карточка стандарта с выключенным расширенным вариантом."""

    @scenario_test
    async def test_card_shows_only_simple_text_and_exact_rows(self, h) -> None:
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)

        self.assertFalse(_flag(h, template_id), "новая площадка: вариант выключен")
        text = h.screen_text()
        cat.assert_shows(self, text, CARD_TITLE_KEY, place_name="Дом культуры")
        self.assertIn(SIMPLE_TITLE, text, "жирный подзаголовок «Простой шаблон»")
        for fragment in SIMPLE_ONLY:
            self.assertIn(fragment, text, "показан текст простого шаблона")

        # §6.2: расширенный текст, технические поля и справка не показываются.
        for fragment in EXTENDED_ONLY:
            self.assertNotIn(fragment, text, "расширенный текст скрыт")
        self.assertNotIn("Тип:", text, "техническое поле «Тип» убрано")
        self.assertNotIn("Версия:", text, "техническое поле «Версия» убрано")
        self.assertNotIn(EXTENDED_ON, text, "строки о включении ещё нет")

        # §6.2: кнопки строго по строкам, «Условных частей» нет.
        self.assertEqual(
            _rows(h),
            [
                [
                    (BTN_EDIT, f"tp_edit:{template_id}"),
                    (BTN_ON, f"tp_ext_on:{template_id}"),
                ],
                [(BTN_BRICKS, f"tp_bricks:{template_id}")],
                [(BTN_RESET, f"tp_reset:{template_id}")],
                [(BTN_TO_PLACE, f"tp_back_place:{place_id}")],
            ],
        )
        self.assertEqual(_callbacks(h, "tp_cond:"), [], "условий у выключенного нет")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class OnCardTests(unittest.TestCase):
    """§6.3–§6.4: включённый вариант и переключение без нового сообщения."""

    @scenario_test
    async def test_enabled_card_content_and_exact_rows(self, h) -> None:
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)
        await h.callback(f"tp_ext_on:{template_id}")

        text = h.screen_text()
        self.assertIn(EXTENDED_ON, text, "строка о включении есть")
        cat.assert_shows(self, text, CARD_TITLE_KEY, place_name="Дом культуры")
        self.assertIn(SIMPLE_TITLE, text, "простой текст остаётся на карточке")
        for fragment in EXTENDED_ONLY:
            self.assertNotIn(fragment, text, "расширенный текст в карточке не виден")
        self.assertNotIn("Тип:", text)
        self.assertNotIn("Версия:", text)

        self.assertEqual(
            _rows(h),
            [
                [
                    (BTN_SIMPLE, f"tp_edit:{template_id}"),
                    (BTN_EXT, f"tp_edit_ext:{template_id}"),
                ],
                [(BTN_COND, f"tp_cond:{template_id}")],
                [(BTN_BRICKS, f"tp_bricks:{template_id}")],
                [
                    (BTN_RESET, f"tp_reset:{template_id}"),
                    (BTN_OFF, f"tp_ext_off:{template_id}"),
                ],
                [(BTN_TO_PLACE, f"tp_back_place:{place_id}")],
            ],
        )

    @scenario_test
    async def test_toggle_edits_current_message_without_success_screen(self, h) -> None:
        """§6.4: одно нажатие — та же карточка, без «Шаблон включён»."""
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)

        messages_before = _message_calls(h)
        screen_id_before = h.screen_message_id()
        answers_before = len(h.answers())
        await h.callback(f"tp_ext_on:{template_id}")

        self.assertTrue(_flag(h, template_id), "флаг включён сразу")
        self.assertEqual(_message_calls(h), messages_before, "новое сообщение")
        self.assertEqual(h.screen_message_id(), screen_id_before, "экран пересоздан")
        self.assertEqual(h.screen()["type"], "edit_message")
        self.assertEqual(len(h.answers()), answers_before + 1, "ровно один ack")
        self.assertIsNone(
            h.answers()[-1].get("text"), "ответ без текста: успех не объявляется"
        )
        self.assertNotIn("Шаблон включён", h.screen_text())
        self.assertIn(EXTENDED_ON, h.screen_text(), "карточка перерисована")

        # Повторное нажатие той же кнопки — не ошибка и не новое сообщение.
        messages_before = _message_calls(h)
        await h.callback(f"tp_ext_on:{template_id}")
        self.assertTrue(_flag(h, template_id))
        self.assertEqual(_message_calls(h), messages_before, "лишнее сообщение")
        cat.assert_not_shows(self, h.screen_text(), "errors.internal")

    @scenario_test
    async def test_texts_and_rules_survive_toggle_cycle(self, h) -> None:
        """§6.4: вкл→выкл не теряет ни тексты, ни условные части."""
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)
        extended_before = _extended_text(h, template_id)
        rules_before = {
            (part.section, part.code, part.fragment) for part in _rules(h, template_id)
        }
        self.assertTrue(rules_before, "у стандарта есть встроенные правила")

        for prefix in ("tp_ext_on", "tp_ext_off", "tp_ext_on", "tp_ext_off"):
            await h.callback(f"{prefix}:{template_id}")

        self.assertFalse(_flag(h, template_id), "цикл закончился выключением")
        self.assertEqual(_extended_text(h, template_id), extended_before)
        self.assertEqual(
            {
                (part.section, part.code, part.fragment)
                for part in _rules(h, template_id)
            },
            rules_before,
        )
        self.assertNotIn(EXTENDED_ON, h.screen_text(), "карточка снова простая")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class EditorScreenTests(unittest.TestCase):
    """§7.2–7.3: экран правки простого и расширенного вариантов."""

    @scenario_test
    async def test_simple_editor_content_and_rows(self, h) -> None:
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)
        await h.callback(f"tp_edit:{template_id}")

        text = h.screen_text()
        self.assertTrue(text.startswith(EDIT_TITLE), text[:80])
        self.assertNotIn(EDIT_TITLE_EXT, text, "заголовок другого варианта")
        self.assertIn(EDIT_CURRENT, text, "подпись «Текущий текст:»")
        for fragment in SIMPLE_ONLY:
            self.assertIn(fragment, text, "сохранённый текст с переносами")
        self.assertIn(EDIT_INSTRUCTION, text, "короткий абзац-инструкция")

        self.assertEqual(
            _rows(h),
            [
                [
                    (BTN_LIST, f"tp_bricks:{template_id}"),
                    (BTN_HELP, f"tp_help:{template_id}"),
                ],
                [(BTN_TO_TEMPLATE, f"tp_open:{template_id}")],
            ],
        )

    @scenario_test
    async def test_extended_editor_has_its_own_title(self, h) -> None:
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)
        await h.callback(f"tp_ext_on:{template_id}")
        await h.callback(f"tp_edit_ext:{template_id}")

        text = h.screen_text()
        self.assertTrue(text.startswith(EDIT_TITLE_EXT), text[:80])
        self.assertIn(EDIT_CURRENT, text)
        for fragment in EXTENDED_ONLY:
            self.assertIn(fragment, text, "показан текст расширенного варианта")
        self.assertIn(EDIT_INSTRUCTION, text)

    @scenario_test
    async def test_clear_scenario_is_gone(self, h) -> None:
        """§7.3: кнопки «Очистить» нет; старая кнопка — безопасный STALE."""
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)
        await h.callback(f"tp_edit:{template_id}")

        labels = [label for row in _rows(h) for label, _data in row]
        self.assertFalse(
            [label for label in labels if "Очистить" in label],
            f"«Очистить» должна исчезнуть: {labels}",
        )
        self.assertEqual(_callbacks(h, "tp_clear"), [], "регистрации кнопки нет")

        screen_before = h.screen_text()
        answers_before = len(h.answers())
        await h.callback("tp_clear")
        self.assertEqual(
            h.answer_texts()[len(h.answer_texts()) - 1 :],
            [STALE_TEMPLATES],
            "устаревшая кнопка объясняет себя",
        )
        self.assertEqual(len(h.answers()), answers_before + 1, "ровно один ответ")
        self.assertEqual(h.screen_text(), screen_before, "экран не изменился")
        cat.assert_not_shows(self, h.screen_text(), "errors.internal")
        # В редакторе остался тот же простой вариант — ничего не очищено.
        self.assertTrue(h.screen_text().startswith(EDIT_TITLE))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class HelpScreenTests(unittest.TestCase):
    """§7.5: короткий экран «Как редактировать» и возврат в редактор."""

    @scenario_test
    async def test_help_content_and_single_back_button(self, h) -> None:
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)
        await h.callback(f"tp_edit:{template_id}")
        await h.callback(f"tp_help:{template_id}")

        text = h.screen_text()
        cat.assert_shows(self, text, "templates.help.title")
        cat.assert_shows(self, text, "templates.help.intro")
        cat.assert_shows(self, text, "templates.help.example")
        cat.assert_shows(self, text, "templates.help.substitution")
        cat.assert_shows(self, text, "templates.help.formats")
        # Пример — отдельным блоком и по действующему синтаксису компилятора.
        example = cat.text("templates.help.example")
        self.assertIn("**[название]**", example)
        self.assertNotIn("__", example, "подчёркивание не показывается как формат")

        self.assertEqual(_rows(h), [[(BTN_TO_EDITOR, "tp_back")]])

        await h.callback("tp_back")
        self.assertTrue(
            h.screen_text().startswith(EDIT_TITLE),
            "возврат в редактор простого варианта",
        )

    @scenario_test
    async def test_help_returns_to_the_same_extended_editor(self, h) -> None:
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)
        await h.callback(f"tp_ext_on:{template_id}")
        await h.callback(f"tp_edit_ext:{template_id}")
        await h.callback(f"tp_help:{template_id}")

        await h.callback("tp_back")
        self.assertTrue(
            h.screen_text().startswith(EDIT_TITLE_EXT),
            "возврат в редактор РАСШИРЕННОГО варианта",
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class BrickListTests(unittest.TestCase):
    """§7.4: список кирпичиков с кратким описанием и возврат в редактор."""

    def _personal_template(self, h) -> tuple[int, str]:
        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="c1-user")
            brick = create_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                code="main",
                title="Основное",
                value_type="short_text",
                rules={},
                description="Короткое пояснение автору",
            )
            template = create_personal_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                name="Личный",
                content="[main]",
                extended_content="[main]\n[main]",
            )
            session.commit()
            return template.template_id, brick.code

    @scenario_test
    async def test_list_shows_code_title_description_and_back(self, h) -> None:
        template_id, code = self._personal_template(h)
        await h.text("/start")
        await _open_personal_card(h, template_id)
        await h.callback(f"tp_edit_ext:{template_id}")
        await h.callback(f"tp_bricks:{template_id}")

        # §14: пять записей на страницу — собираем все страницы списка.
        listed: list[str] = [h.screen_text()]
        seen: set[int] = {0}
        while True:
            candidates = {
                int(data.split(":", 1)[1])
                for _label, data in h.screen_buttons()
                if data and data.startswith("tp_bpage:")
            } - seen
            if not candidates:
                break
            offset = min(candidates)
            seen.add(offset)
            await h.callback(f"tp_bpage:{offset}")
            listed.append(h.screen_text())
        text = "\n".join(listed)

        cat.assert_shows(self, text, "templates.brick_list.dostupnye_kirpichiki")
        self.assertIn(f"[{code}] — ", text, "код в квадратных скобках и название")
        self.assertIn("Основное", text)
        self.assertIn("Короткое пояснение автору", text, "краткое описание")

        await h.callback("tp_back")
        self.assertTrue(
            h.screen_text().startswith(EDIT_TITLE_EXT),
            "«Назад» вернул в редактор того же варианта",
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AntiRegressionTests(unittest.TestCase):
    """Правка и сохранение текста, восстановление стандарта из карточки."""

    @scenario_test
    async def test_edit_and_save_still_works(self, h) -> None:
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)
        await h.callback(f"tp_edit:{template_id}")

        await h.text("Новый простой текст [название]")
        await h.callback("tp_save")

        with h.session_factory() as session:
            standard = get_standard_template(session, DEFAULT_USER_ID, place_id)
            revision = standard.revision
            saved = get_template_version(
                session, DEFAULT_USER_ID, template_id, revision
            )
        assert saved is not None
        self.assertEqual(saved.content, "Новый простой текст [название]")

        await h.callback("tp_back")
        card = h.screen_text()
        self.assertIn("Новый простой текст [название]", card, "карточка обновилась")
        # Вариант не включали: карточка остаётся простой, но текст новый.
        self.assertNotIn(EXTENDED_ON, card)

    @scenario_test
    async def test_restore_standard_is_available_on_card(self, h) -> None:
        place_id = _seed_place(h)
        await h.text("/start")
        template_id = await _open_card(h, place_id)

        self.assertIn(f"tp_reset:{template_id}", _callbacks(h, "tp_reset:"))
        self.assertIn(BTN_RESET, _labels(h))
