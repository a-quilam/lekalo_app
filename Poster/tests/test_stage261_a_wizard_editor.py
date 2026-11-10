# tests/test_stage261_a_wizard_editor.py
"""
Poster 2.6.1, этап A — независимая приёмка экранного контракта.

Проверяются ТРЕБОВАНИЯ ВЛАДЕЛЬЦА (`docs/agent_team/task_261_20261110.md`,
§3 и §4), а не устройство реализации. Все сценарии идут через настоящее
приложение (`bot.build_application`), `Application.process_update`, FakeBot
и временную SQLite (`tests/integration_harness.py`): рабочая `post_bot.db`,
`.env` и сеть Telegram не используются.

Что закрепляется.

§3.1 — экран вопроса состоит ровно из двух блоков: шапка
`🧩 🏷 Название · 1/5` и через пустую строку одна подсказка. Ни описания
типа вторым блоком, ни служебных строк «Сейчас: …», «Поле пропущено»,
«Поле можно пропустить».

§3.2 — в шапке короткие названия системных кирпичиков (Название, Дата,
Начало, Окончание, Описание, Контакт, Условие входа, Куратор, Подвал) с
прежними эмодзи типов; у пользовательских кирпичиков — сохранённое название.

§3.3 — подсказка РОВНО одна: описание кирпичика, иначе типовая подсказка
(дата/время/выбор), иначе короткий fallback «Укажите значение поля „{название}“.».
Дубль «Напишите название мероприятия» + «Напишите короткий текст» запрещён.

§3.4 — счётчик считается по фактической последовательности вопросов:
длина шаблона, пропуски, «Назад», кнопка «Пропустить» и автоподставленный
единственный подвал (вопрос по нему не задаётся — счётчик не растёт).

§4 — кнопка «🧩 Дополнительные поля» в главном редакторе появляется только
при включённом расширенном варианте шаблона площадки; у личного шаблона
поведение прежнее; устаревшая кнопка получает короткий alert, не меняя
экран и не создавая сообщений.

Антирегрессии: живой анонс приходит РАНЬШЕ вопроса; при ошибке ввода
сообщение пользователя не удаляется, а экран показывает конкретную причину.

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage261_a_wizard_editor -v
"""

from __future__ import annotations

import re
import unittest
from unittest.mock import patch

from tests import ui_texts_expect as cat

try:
    from announcement_store import (
        extras_available,
        extras_available_for_draft,
        get_announcement_draft,
    )
    from brick_types import BrickValueStatus
    from catalogs_store import create_curator, create_footer
    from template_store import (
        get_standard_template,
        set_standard_extended_active,
        update_template_content,
    )
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        assert_delivered,
        is_live_record,
        is_panel_record,
        is_screen_record,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        _choose_personal,
        _latest_draft,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


# --- литералы владельца (task_261 §3–§4) -------------------------------------

# §3.1: эталон шапки вопроса — «эмодзи поля, короткое имя, счётчик».
HEADER_RE = re.compile(r"^🧩 (?P<title>.+) · (?P<step>\d+)/(?P<total>\d+)$")
NAME_TITLE = "🏷 Название"
DATE_TITLE = "📅 Дата"
TIME_START_TITLE = "🕒 Начало"
TIME_END_TITLE = "🕒 Окончание"
DESCRIPTION_TITLE = "📝 Описание"
CONTACT_TITLE = "☎️ Контакт"
ENTRY_TITLE = "🎟 Условие входа"
CURATOR_TITLE = "🧑‍💼 Куратор"
FOOTER_TITLE = "📌 Подвал"

EXTRAS_LABEL = "🧩 Дополнительные поля"
DATA_LABEL = "📝 Текстовые поля"
MENU_LABEL = "🏠 В меню"
PHOTOS_LABEL = "🖼 Фото (0)"

# Подсказки типов: по ним проверяется §3.3 (что показано и чего нет).
SHORT_TEXT_HINT_KEYS = (
    "wizard.hint.short_text",
    "wizard.hint.long_text",
)
FALLBACK_KEY = "wizard.hint.fallback"
DISABLED_EXTRAS_KEY = "wizard.extras.disabled"

TemplateSpec = tuple[str, str, str, dict[str, object]]


# --- вспомогательные функции -------------------------------------------------


def _blocks(screen: str) -> list[str]:
    """Блоки экрана: ровно то, что пользователь видит между пустыми строками."""
    return (screen or "").split("\n\n")


def _header(screen: str) -> str:
    return _blocks(screen)[0]


def _step_total(screen: str) -> tuple[int, int]:
    match = HEADER_RE.match(_header(screen))
    if match is None:
        raise AssertionError(f"экран не вопроса: {screen[:200]!r}")
    return int(match.group("step")), int(match.group("total"))


def _header_title(screen: str) -> str:
    match = HEADER_RE.match(_header(screen))
    if match is None:
        raise AssertionError(f"экран не вопроса: {screen[:200]!r}")
    return match.group("title")


def _expected_header(step: int, total: int, title: str) -> str:
    """Шапка ровно по формату §3.1 (и сверка с каталогом ниже в тестах)."""
    return f"🧩 {title} · {step}/{total}"


def _seed_standard(
    h,
    place_id: int,
    *,
    content: str | None = None,
    extended_content: str | None = None,
    extended_active: bool | None = None,
) -> int:
    """Готовит стандартный шаблон площадки и возвращает его id."""
    with h.session_factory() as session:
        standard = get_standard_template(session, DEFAULT_USER_ID, place_id)
        revision = standard.revision
        if content is not None:
            result = update_template_content(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                template_id=standard.template_id,
                expected_revision=revision,
                content=content,
                extended_content=extended_content,
            )
            if not result.ok or result.template is None:
                raise AssertionError(f"шаблон не обновлён: {result.message}")
            revision = result.template.revision
        if extended_active is not None:
            set_standard_extended_active(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                template_id=standard.template_id,
                active=extended_active,
                expected_revision=revision,
            )
        session.commit()
        return int(standard.template_id)


def _toggle_standard_extended(h, place_id: int, active: bool) -> None:
    """Переключает флаг расширенного варианта ПОСЛЕ начала сценария (§4)."""
    with h.session_factory() as session:
        standard = get_standard_template(session, DEFAULT_USER_ID, place_id)
        set_standard_extended_active(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            template_id=standard.template_id,
            active=active,
            expected_revision=standard.revision,
        )
        session.commit()


def _seed_footer(h, place_id: int | None, name: str, text: str) -> None:
    with h.session_factory() as session:
        create_footer(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            place_id=place_id,
            name=name,
            text=text,
        )
        session.commit()


def _seed_curator(h, place_id: int | None, value: str) -> None:
    with h.session_factory() as session:
        create_curator(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            place_id=place_id,
            value=value,
        )
        session.commit()


async def _place(h, **kwargs) -> int:
    """
    Создаёт площадку автора и возвращает её id.

    `_seed_place` из 27B ищет уже существующего пользователя, поэтому сначала
    выполняется штатный `/start` — как у живого автора.
    """
    await h.text("/start")
    place_id, _alias = _seed_place(h, **kwargs)
    return place_id


async def _open_place_questions(h, place_id: int) -> None:
    """Главное меню → площадка → первый вопрос стандартного шаблона."""
    await h.text("/start")
    await h.callback("pick_place")
    await h.callback(f"pl_pick:{place_id}")


async def _open_personal_questions(h, place_id: int, template_id: int) -> None:
    """Главное меню → площадка → личный шаблон → первый вопрос."""
    await h.text("/start")
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)


async def _walk_questions(
    h,
    *,
    prefer: str = "answer",
    answer: str = "Значение",
    limit: int = 24,
) -> list[str]:
    """
    Проходит вопросы мастера до шага фотографий.

    Возвращает тексты экранов вопросов в порядке показа. Спецкнопки
    (календарь, быстрое время, варианты выбора, справочники) нажимаются как
    живой пользователь; `prefer="skip"` использует «⏭ Пропустить» там, где
    ручной ввод запрещён (подвал пустого справочника).
    """
    screens: list[str] = []
    for _ in range(limit):
        data = [value for _label, value in h.screen_buttons() if value]
        if any(value.startswith("aw_photo_none") for value in data):
            return screens
        screens.append(h.screen_text())
        pick = next(
            (
                value
                for value in data
                if value.startswith(("aw_curpick", "aw_footpick"))
            ),
            None,
        )
        preset = next((value for value in data if value.startswith("aw_timeset")), None)
        day = next((value for value in data if value.startswith("aw_calday:")), None)
        choice = next((value for value in data if value.startswith("aw_choice:")), None)
        skip = next((value for value in data if value.startswith("aw_qskip")), None)
        if pick is not None:
            await h.callback(pick)
        elif preset is not None:
            await h.callback(preset)
        elif day is not None:
            await h.callback(day)
        elif choice is not None:
            await h.callback(choice)
        elif prefer == "skip" and skip is not None:
            await h.callback(skip)
        else:
            await h.text(answer)
    raise AssertionError(f"мастер не дошёл до фотографий: {h.screen_text()[:200]!r}")


async def _finish_without_photos(h) -> None:
    data = [value for _label, value in h.screen_buttons() if value]
    none = next((value for value in data if value.startswith("aw_photo_none")), None)
    if none is None:
        raise AssertionError(f"не шаг фотографий: {h.screen_text()[:200]!r}")
    await h.callback(none)


async def _deliver(h, *, prefer: str = "answer") -> tuple[int, list[str]]:
    """Проходит мастер без фото и возвращает (draft_id, экраны вопросов)."""
    screens = await _walk_questions(h, prefer=prefer)
    await _finish_without_photos(h)
    draft = _latest_draft(h)
    assert_delivered(h, draft.id)
    return int(draft.id), screens


def _rows_of(markup) -> list[list[tuple[str, str | None]]]:
    rows = getattr(markup, "inline_keyboard", None) or []
    return [[(button.text, button.callback_data) for button in row] for row in rows]


def _control_rows(h) -> list[list[tuple[str, str | None]]]:
    """Строки клавиатуры управления доставленным анонсом (0–1 фото)."""
    for record in reversed(h.calls):
        rows = _rows_of(record.get("reply_markup"))
        data = [value for row in rows for _label, value in row]
        if data and all(value and value.startswith("anp_") for value in data):
            return rows
    raise AssertionError(f"в чате нет управления анонсом: {h.live_buttons()}")


def _panel_rows(h) -> list[list[tuple[str, str | None]]]:
    """Строки отдельной панели управления медиагруппой (2+ фото)."""
    records = [record for record in h.calls if is_panel_record(record)]
    if not records:
        raise AssertionError("отдельной панели управления нет")
    return _rows_of(records[-1].get("reply_markup"))


def _labels(rows) -> list[str]:
    return [label for row in rows for label, _data in row]


def _new_content_calls(h, since: int) -> list[dict]:
    """Вызовы Telegram, создающие/меняющие сообщения после индекса `since`."""
    kinds = {
        "message",
        "photo",
        "media_group",
        "rich_message",
        "document",
        "edit_message",
        "edit_markup",
        "edit_caption",
        "edit_media",
        "delete_message",
    }
    return [call for call in h.calls[since:] if call["type"] in kinds]


def _details(h, draft_id: int):
    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
        )


def _binding(details, code: str) -> int:
    snapshot = details.template_snapshot
    assert snapshot is not None, "у черновика нет снимка шаблона"
    for binding in snapshot.bindings:
        if binding.code == code:
            return int(binding.brick_id)
    raise AssertionError(f"в снимке нет кирпичика [{code}]")


# --- §3.1: два блока ---------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class QuestionFormatTests(unittest.TestCase):
    """§3.1: шапка, ровно одна пустая строка, ровно одна подсказка."""

    @scenario_test
    async def test_first_question_is_exactly_header_and_one_hint(self, h) -> None:
        place_id = await _place(h)
        # Юмор — отдельная функция S5; контракт §3.1 проверяется на чистом
        # экране, иначе редкая шутка добавит третий блок.
        with patch("config.UI_HUMOR_ENABLED", False):
            await _open_place_questions(h, place_id)
            screen = h.screen_text()

        self.assertEqual(
            _blocks(screen),
            [
                _expected_header(1, 5, NAME_TITLE),
                cat.text(
                    "wizard.question.description_html",
                    description="Напишите название мероприятия.",
                ),
            ],
            f"экран вопроса не из двух блоков: {screen!r}",
        )
        # Эталон владельца дословно и совпадение с каталогом формулировок.
        self.assertEqual(_header(screen), "🧩 🏷 Название · 1/5")
        self.assertEqual(
            _header(screen),
            cat.text(
                "wizard.question.header",
                step=1,
                total=5,
                title=NAME_TITLE,
            ),
            "шапка расходится с каталогом",
        )
        self.assertEqual(screen.count("\n\n"), 1, "лишние пустые строки")
        self.assertEqual(screen, screen.strip(), "пустые строки по краям")

    @scenario_test
    async def test_question_has_no_service_lines_or_duplicate_hint(self, h) -> None:
        place_id = await _place(h)
        await _open_place_questions(h, place_id)
        await h.text("Читка пьесы")
        screen = h.screen_text()

        # §3.3: дубль «Напишите название мероприятия» + «Напишите короткий
        # текст» — дефект, который устраняет контракт.
        for key in SHORT_TEXT_HINT_KEYS:
            cat.assert_not_shows(self, screen, key)
        # Служебных строк о значении и пропуске на экране вопроса нет.
        for key in (
            "wizard.question.current_value",
            "wizard.question.skipped",
        ):
            cat.assert_not_shows(self, screen, key)
        self.assertNotIn("✓", screen, "служебная отметка заполненного поля")
        # Строки «поле можно пропустить» в каталоге больше нет (§3.3): ни
        # текстом, ни пустым блоком она появляться не должна.
        self.assertNotIn("можно пропустить", screen.lower())
        self.assertNotIn("пропустить поле", screen.lower())
        self.assertNotIn("\n\n\n", screen, "пустой блок оставил строку-разделитель")
        self.assertFalse(
            [_block for _block in _blocks(screen) if not _block.strip()],
            f"на экране есть пустой блок: {_blocks(screen)!r}",
        )


# --- §3.3: единственная подсказка -------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class QuestionHintTests(unittest.TestCase):
    """§3.3: приоритет подсказки — описание, правила типа, fallback."""

    @scenario_test
    async def test_description_is_the_only_hint(self, h) -> None:
        """Системное поле с описанием: показано только описание."""
        place_id = await _place(h)
        await _open_place_questions(h, place_id)
        blocks = _blocks(h.screen_text())
        self.assertEqual(len(blocks), 2, f"блоков больше двух: {blocks!r}")
        self.assertIn("Напишите название мероприятия.", blocks[1])
        # Типовая подсказка простого текста НЕ добавлена.
        cat.assert_not_shows(self, h.screen_text(), "wizard.hint.short_text")
        cat.assert_not_shows(self, h.screen_text(), FALLBACK_KEY)

    @scenario_test
    async def test_time_field_shows_type_hint_without_fallback(self, h) -> None:
        place_id = await _place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Шаблон со временем",
            specs=[("t1", "Время встречи", "time", {})],
            content="[t1]",
        )
        await _open_personal_questions(h, place_id, template_id)
        screen = h.screen_text()
        blocks = _blocks(screen)
        self.assertEqual(len(blocks), 2, f"блоков больше двух: {blocks!r}")
        self.assertEqual(
            blocks[1], cat.text("wizard.hint.time", presets="18:00, 18:30, 19:00")
        )
        cat.assert_not_shows(self, screen, FALLBACK_KEY)

    @scenario_test
    async def test_date_field_shows_calendar_hint_without_rules(self, h) -> None:
        """Дата без правил: подсказка календаря есть всегда (календарь на экране)."""
        place_id = await _place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Шаблон с датой",
            specs=[("d1", "Дата встречи", "date", {})],
            content="[d1]",
        )
        await _open_personal_questions(h, place_id, template_id)
        screen = h.screen_text()
        self.assertEqual(len(_blocks(screen)), 2, f"блоков больше двух: {screen!r}")
        self.assertEqual(_blocks(screen)[1], cat.text("wizard.hint.date"))
        cat.assert_not_shows(self, screen, FALLBACK_KEY)

    @scenario_test
    async def test_choice_field_shows_type_hint_without_fallback(self, h) -> None:
        place_id = await _place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Шаблон с выбором",
            specs=[("c1", "Формат", "choice", {"choices": ["Оффлайн", "Онлайн"]})],
            content="[c1]",
        )
        await _open_personal_questions(h, place_id, template_id)
        screen = h.screen_text()
        self.assertEqual(len(_blocks(screen)), 2)
        self.assertEqual(_blocks(screen)[1], cat.text("wizard.hint.choice_single"))
        cat.assert_not_shows(self, screen, FALLBACK_KEY)

    @scenario_test
    async def test_field_without_description_and_rules_uses_fallback(self, h) -> None:
        place_id = await _place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Шаблон с fallback",
            specs=[("p1", "Первый вопрос", "short_text", {})],
            content="[p1]",
        )
        await _open_personal_questions(h, place_id, template_id)
        screen = h.screen_text()
        self.assertEqual(
            _blocks(screen),
            [
                "🧩 Первый вопрос · 1/1",
                cat.text(FALLBACK_KEY, title="Первый вопрос"),
            ],
            f"fallback сформулирован иначе: {screen!r}",
        )
        self.assertIn("Укажите значение поля „Первый вопрос“.", screen)

    @scenario_test
    async def test_catalog_curator_question_has_single_hint(self, h) -> None:
        """
        §3.1/§3.3: у вопроса с описанием не бывает ВТОРОЙ подсказки.

        Раньше экран вопроса куратора добавлял к описанию кирпичика ещё и
        `wizard.catalog.curator_choose` («Выберите куратора или пришлите его
        контакт.») — почти дословный дубль описания. Теперь строка каталога
        добавляется только при отсутствии описания (§3.3), поэтому у
        системного кирпичика остаётся ровно одна подсказка.
        """
        place_id = await _place(h)
        # Расширенный вариант включается ДО начала сценария: снимок шаблона
        # берётся при входе в мастер.
        _seed_standard(h, place_id, extended_active=True)
        await _open_place_questions(h, place_id)
        screens = await _walk_questions(h, prefer="skip")

        curator_screens = [
            screen for screen in screens if _header_title(screen) == CURATOR_TITLE
        ]
        self.assertTrue(curator_screens, f"вопрос куратора не показан: {screens!r}")
        self.assertEqual(
            len(_blocks(curator_screens[0])),
            2,
            f"у вопроса куратора больше одной подсказки: {curator_screens[0]!r}",
        )


# --- §3.4: динамический счётчик ---------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class QuestionCounterTests(unittest.TestCase):
    """§3.4: счётчик равен фактической последовательности вопросов."""

    @scenario_test
    async def test_four_question_template_counts_four(self, h) -> None:
        place_id = await _place(h)
        specs: list[TemplateSpec] = [
            ("q1", "Первый", "short_text", {}),
            ("q2", "Второй", "short_text", {}),
            ("q3", "Третий", "short_text", {}),
            ("q4", "Четвёртый", "short_text", {}),
        ]
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Четыре вопроса",
            specs=specs,
            content=" ".join(f"[{code}]" for code, _t, _v, _r in specs),
        )
        await _open_personal_questions(h, place_id, template_id)
        screens = await _walk_questions(h)

        self.assertEqual(
            [_step_total(screen) for screen in screens],
            [(1, 4), (2, 4), (3, 4), (4, 4)],
            f"счётчик не совпал с числом вопросов: {screens!r}",
        )

    @scenario_test
    async def test_six_question_template_counts_six(self, h) -> None:
        place_id = await _place(h)
        specs: list[TemplateSpec] = [
            (f"q{index}", f"Поле {index}", "short_text", {}) for index in range(1, 7)
        ]
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Шесть вопросов",
            specs=specs,
            content=" ".join(f"[{code}]" for code, _t, _v, _r in specs),
        )
        await _open_personal_questions(h, place_id, template_id)
        screens = await _walk_questions(h)

        self.assertEqual(
            [_step_total(screen) for screen in screens],
            [(step, 6) for step in range(1, 7)],
            f"счётчик не совпал с числом вопросов: {screens!r}",
        )

    @scenario_test
    async def test_skip_does_not_change_total(self, h) -> None:
        place_id = await _place(h)
        specs: list[TemplateSpec] = [
            (f"s{index}", f"Поле {index}", "short_text", {}) for index in range(1, 5)
        ]
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Пропуски",
            specs=specs,
            content=" ".join(f"[{code}]" for code, _t, _v, _r in specs),
        )
        await _open_personal_questions(h, place_id, template_id)
        screens = await _walk_questions(h, prefer="skip")

        self.assertEqual(
            [_step_total(screen) for screen in screens],
            [(1, 4), (2, 4), (3, 4), (4, 4)],
            f"«Пропустить» изменил общее количество: {screens!r}",
        )

    @scenario_test
    async def test_back_shows_correct_step(self, h) -> None:
        place_id = await _place(h)
        specs: list[TemplateSpec] = [
            (f"b{index}", f"Поле {index}", "short_text", {}) for index in range(1, 5)
        ]
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Возврат",
            specs=specs,
            content=" ".join(f"[{code}]" for code, _t, _v, _r in specs),
        )
        await _open_personal_questions(h, place_id, template_id)
        await h.text("Значение 1")
        await h.text("Значение 2")
        self.assertEqual(_step_total(h.screen_text()), (3, 4), h.screen_text())

        await h.callback(
            next(
                data
                for _label, data in h.screen_buttons()
                if data and data.startswith("aw_qback")
            )
        )
        self.assertEqual(_step_total(h.screen_text()), (2, 4), h.screen_text())
        self.assertEqual(_header_title(h.screen_text()), "Поле 2")

        await h.text("Значение 2 заново")
        self.assertEqual(_step_total(h.screen_text()), (3, 4), h.screen_text())

    @scenario_test
    async def test_autofilled_footer_is_not_a_question(self, h) -> None:
        """
        Единственный активный подвал подставляется автоматически.

        Вопрос по нему пользователю не задаётся, поэтому общее количество
        остаётся 6, а не 7; значение при этом сохранено в анонсе.
        """
        place_id = await _place(h)
        _seed_footer(h, place_id, "Наш подвал", "До встречи!")
        _seed_standard(h, place_id, extended_active=True)
        await _open_place_questions(h, place_id)
        screens = await _walk_questions(h)

        self.assertEqual(
            [_step_total(screen) for screen in screens],
            [(step, 6) for step in range(1, 7)],
            f"автоподставленный подвал увеличил счётчик: {screens!r}",
        )
        self.assertNotIn(
            FOOTER_TITLE,
            [_header_title(screen) for screen in screens],
            "по автоподставленному подвалу задан вопрос",
        )

        draft_id, _screens = await _deliver(h)
        details = _details(h, draft_id)
        footer = details.value_for(_binding(details, "подвал"))
        self.assertIsNotNone(footer, "подвал не подставлен")
        assert footer is not None
        self.assertIs(footer.status, BrickValueStatus.VALID)

    @scenario_test
    async def test_footer_without_catalog_is_counted(self, h) -> None:
        """Пустой справочник подвалов — настоящий вопрос: он в счётчике."""
        place_id = await _place(h)
        _seed_standard(
            h,
            place_id,
            content="**[название]**\n\n[подвал]",
            extended_content=None,
        )
        await _open_place_questions(h, place_id)
        screens = await _walk_questions(h, prefer="skip")

        self.assertEqual(
            [(1, 2), (2, 2)],
            [_step_total(screen) for screen in screens],
            f"подвал без справочника не посчитан: {screens!r}",
        )
        self.assertEqual(_header_title(screens[1]), FOOTER_TITLE)

    @scenario_test
    async def test_standard_five_and_extended_six_question_shapes(self, h) -> None:
        """
        Стандарт: простой вариант — 5 вопросов, расширенный — 6.

        В расширенном варианте есть `[подвал]`; активный подвал в справочнике
        один, поэтому он подставляется сам и вопросом не становится (§3.4).
        """
        place_id = await _place(h)
        _seed_footer(h, place_id, "Наш подвал", "До встречи!")
        await _open_place_questions(h, place_id)
        simple = await _walk_questions(h, prefer="skip")
        self.assertEqual(
            [_step_total(screen) for screen in simple],
            [(n, 5) for n in range(1, 6)],
            f"простой шаблон считает не 5 вопросов: {simple!r}",
        )
        await _finish_without_photos(h)

        _toggle_standard_extended(h, place_id, True)
        await _open_place_questions(h, place_id)
        extended = await _walk_questions(h, prefer="skip")
        self.assertEqual(
            [_step_total(screen) for screen in extended],
            [(n, 6) for n in range(1, 7)],
            f"расширенный шаблон считает не 6 вопросов: {extended!r}",
        )


# --- §4: гейт дополнительных полей ------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ExtrasGateTests(unittest.TestCase):
    """§4: кнопка доп. полей есть только у включённого расширенного."""

    @scenario_test
    async def test_disabled_extended_hides_extras_in_editor(self, h) -> None:
        """Стандарт с расширенным текстом, но флаг выключен — кнопки нет."""
        place_id = await _place(h)
        _seed_standard(
            h,
            place_id,
            content="**[название]**",
            extended_content="**[название]**\n\nКуратор: [куратор]",
            extended_active=False,
        )
        await _open_place_questions(h, place_id)
        draft_id, _screens = await _deliver(h)

        self.assertEqual(
            _control_rows(h),
            [
                [
                    (DATA_LABEL, f"anp_data:{draft_id}"),
                    (PHOTOS_LABEL, f"anp_photos:{draft_id}"),
                ],
                [(MENU_LABEL, f"anp_menu:{draft_id}")],
            ],
            f"клавиатура редактора не по контракту: {_control_rows(h)}",
        )
        self.assertNotIn(EXTRAS_LABEL, _labels(_control_rows(h)))
        self.assertFalse(
            [
                data
                for _label, data in h.live_buttons()
                if data and data.startswith("anp_extras")
            ],
            "переход в доп. поля остался на живых клавиатурах",
        )
        details = _details(h, draft_id)
        with h.session_factory() as session:
            self.assertFalse(
                extras_available(
                    session, details, actor_telegram_user_id=DEFAULT_USER_ID
                ),
                "расширенный текст в БД не делает поля доступными",
            )
            self.assertFalse(extras_available_for_draft(session, draft_id))

    @scenario_test
    async def test_enabled_extended_shows_extras_second_row(self, h) -> None:
        place_id = await _place(h)
        _seed_standard(
            h,
            place_id,
            content="**[название]**",
            extended_content="**[название]**\n\nКуратор: [куратор]",
            extended_active=True,
        )
        await _open_place_questions(h, place_id)
        draft_id, _screens = await _deliver(h)

        self.assertEqual(
            _control_rows(h),
            [
                [
                    (DATA_LABEL, f"anp_data:{draft_id}"),
                    (PHOTOS_LABEL, f"anp_photos:{draft_id}"),
                ],
                [(EXTRAS_LABEL, f"anp_extras:{draft_id}")],
                [(MENU_LABEL, f"anp_menu:{draft_id}")],
            ],
            f"кнопка доп. полей не второй строкой: {_control_rows(h)}",
        )

    @scenario_test
    async def test_album_panel_follows_same_gate(self, h) -> None:
        place_id = await _place(h)
        _seed_standard(
            h,
            place_id,
            content="**[название]**",
            extended_content="**[название]**\n\nКуратор: [куратор]",
            extended_active=False,
        )
        await _open_place_questions(h, place_id)
        await _walk_questions(h, prefer="skip")
        for index in range(3):
            await h.photo(f"photo-{index}", media_group_id="album")
        import asyncio

        await asyncio.sleep(0.4)
        draft = _latest_draft(h)
        assert_delivered(h, draft.id)

        rows = _panel_rows(h)
        self.assertEqual([len(row) for row in rows], [2, 1], f"панель: {rows}")
        self.assertNotIn(EXTRAS_LABEL, _labels(rows))
        self.assertEqual(rows[1][0][0], MENU_LABEL, f"панель: {rows}")

    @scenario_test
    async def test_personal_template_keeps_previous_behaviour(self, h) -> None:
        """Личный шаблон: доступность решают его собственные правила (§4)."""
        place_id = await _place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Личный с расширением",
            specs=[
                ("main", "Основное поле", "short_text", {}),
                ("extra", "Стоимость", "short_text", {}),
            ],
            content="[main]",
            extended_content="[main]\n[extra]",
        )
        await _open_personal_questions(h, place_id, template_id)
        await _deliver(h)
        self.assertIn(EXTRAS_LABEL, _labels(_control_rows(h)))

        # Шаблон без расширенного текста — кнопки нет (2.5.2 §4 без изменений).
        await h.text("/start")
        plain_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Личный простой",
            specs=[("only", "Одно поле", "short_text", {})],
            content="[only]",
            extended_content=None,
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, plain_id)
        await _deliver(h)
        self.assertNotIn(EXTRAS_LABEL, _labels(_control_rows(h)))

    @scenario_test
    async def test_stale_question_extras_button_answers_without_changes(
        self, h
    ) -> None:
        """Старая кнопка `aw_extras` при выключенном расширенном — alert."""
        place_id = await _place(h)
        # Форма вопросов задана явно: два поля, чтобы продолжение сценария
        # после alert проверялось предсказуемо (2/2, а не шаг фото).
        _seed_standard(
            h,
            place_id,
            content="**[название]**\n\n[описание]",
            extended_content="**[название]**\n\n[описание]\n\nКуратор: [куратор]",
            extended_active=False,
        )
        await _open_place_questions(h, place_id)
        ui = h.user_data().get("announcement_ui") or {}
        draft_id, revision = int(ui["draft_id"]), int(ui["revision"])
        before_screen = h.screen_text()
        before_buttons = h.screen_buttons()
        before_answers = len(h.answers())
        before_calls = len(h.calls)
        before_deletes = len(h.deleted_ids())
        before_state = h.dialog_state()

        await h.callback(f"aw_extras:{draft_id}:{revision}")

        answers = h.answers()[before_answers:]
        self.assertEqual(len(answers), 1, f"ожидался один ответ: {answers}")
        self.assertEqual(answers[0].get("text"), cat.text(DISABLED_EXTRAS_KEY))
        self.assertTrue(answers[0].get("show_alert"), "alert не показан")
        self.assertEqual(_new_content_calls(h, before_calls), [], "экран изменён")
        self.assertEqual(h.screen_text(), before_screen, "текст экрана изменился")
        self.assertEqual(h.screen_buttons(), before_buttons, "клавиатура изменилась")
        self.assertEqual(len(h.deleted_ids()), before_deletes, "сообщения удалены")
        self.assertEqual(h.dialog_state(), before_state, "состояние диалога сброшено")
        # Сценарий продолжается: ответ на вопрос ведёт к следующему вопросу.
        await h.text("Читка пьесы")
        self.assertEqual(_step_total(h.screen_text()), (2, 2), h.screen_text())

    @scenario_test
    async def test_stale_panel_extras_button_answers_without_changes(self, h) -> None:
        """
        Старая кнопка `anp_extras` при выключенном расширенном — короткий alert.

        Кнопка могла остаться в чате от клавиатуры, нарисованной до
        переключения шаблона (или до этапа A). Снимок черновика фиксирует
        выключенный флаг, поэтому действие обязано отказать, не меняя экран.
        """
        place_id = await _place(h)
        _seed_standard(
            h,
            place_id,
            content="**[название]**",
            extended_content="**[название]**\n\nКуратор: [куратор]",
            extended_active=False,
        )
        await _open_place_questions(h, place_id)
        draft_id, _screens = await _deliver(h)
        # По контракту §4 кнопки на актуальной клавиатуре уже нет.
        self.assertNotIn(
            EXTRAS_LABEL,
            [label for label, _data in h.live_buttons()],
            "кнопка доп. полей нарисована при выключенном расширенном",
        )
        before_buttons = h.live_buttons()
        before_calls = len(h.calls)
        before_answers = len(h.answers())
        before_texts = len(h.answer_texts())
        before_screen = h.screen_text()

        await h.callback(f"anp_extras:{draft_id}")

        texts = h.answer_texts()[before_texts:]
        self.assertIn(
            cat.text(DISABLED_EXTRAS_KEY),
            texts,
            f"нет короткого объяснения: {texts}",
        )
        alerts = [
            call
            for call in h.answers()[before_answers:]
            if call.get("text") == cat.text(DISABLED_EXTRAS_KEY)
        ]
        self.assertTrue(alerts and alerts[-1].get("show_alert"), "ответ не alert")
        self.assertEqual(_new_content_calls(h, before_calls), [], "экран изменён")
        self.assertEqual(h.screen_text(), before_screen, "экран испорчен")
        self.assertEqual(
            h.live_buttons(), before_buttons, "клавиатура управления изменилась"
        )

    @scenario_test
    async def test_started_scenario_keeps_its_snapshot_after_flag_flip(self, h) -> None:
        """§4: начатый до переключения сценарий живёт по своему снимку."""
        place_id = await _place(h)
        _seed_standard(
            h,
            place_id,
            content="**[название]**",
            extended_content="**[название]**\n\nКуратор: [куратор]",
            extended_active=True,
        )
        await _open_place_questions(h, place_id)
        _toggle_standard_extended(h, place_id, False)

        draft_id, _screens = await _deliver(h)
        self.assertIn(
            EXTRAS_LABEL,
            _labels(_control_rows(h)),
            "снимок шаблона должен сохранить доступность доп. полей",
        )
        with h.session_factory() as session:
            details = _details(h, draft_id)
            self.assertTrue(
                extras_available(
                    session, details, actor_telegram_user_id=DEFAULT_USER_ID
                )
            )


# --- §3.2: короткие названия -------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ShortTitleTests(unittest.TestCase):
    """§3.2: в шапке короткое имя системного поля и сохранённое — личного."""

    @scenario_test
    async def test_standard_short_titles_and_emoji(self, h) -> None:
        place_id = await _place(h)
        await _open_place_questions(h, place_id)
        screens = await _walk_questions(h)

        self.assertEqual(
            [_header_title(screen) for screen in screens],
            [
                NAME_TITLE,
                DATE_TITLE,
                TIME_START_TITLE,
                DESCRIPTION_TITLE,
                CONTACT_TITLE,
            ],
            f"короткие названия не по контракту: {screens!r}",
        )

    @scenario_test
    async def test_extended_only_fields_use_short_titles(self, h) -> None:
        place_id = await _place(h)
        _seed_curator(h, place_id, "@admin")
        _seed_standard(
            h,
            place_id,
            content="**[название]**\n\n[время_окончания], [условие_входа]",
            extended_content=None,
        )
        await _open_place_questions(h, place_id)
        screens = await _walk_questions(h)

        self.assertEqual(
            [_header_title(screen) for screen in screens],
            [NAME_TITLE, TIME_END_TITLE, ENTRY_TITLE],
            f"короткие названия не по контракту: {screens!r}",
        )

    @scenario_test
    async def test_personal_brick_keeps_saved_title(self, h) -> None:
        place_id = await _place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Личный с длинным названием",
            specs=[("long", "Стоимость участия в рублях", "short_text", {})],
            content="[long]",
        )
        await _open_personal_questions(h, place_id, template_id)
        screen = h.screen_text()
        self.assertEqual(_header_title(screen), "Стоимость участия в рублях")
        self.assertEqual(_header(screen), "🧩 Стоимость участия в рублях · 1/1")


# --- антирегрессии -----------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AntiRegressionTests(unittest.TestCase):
    """§3.5 и §3.3: живой анонс выше вопроса, ввод при ошибке не теряется."""

    @scenario_test
    async def test_live_announcement_arrives_before_question(self, h) -> None:
        place_id = await _place(h)
        await _open_place_questions(h, place_id)

        live_ids = [
            message_id
            for call in h.calls
            if is_live_record(call)
            for message_id in h.bot.call_message_ids.get(id(call), ())
        ]
        self.assertTrue(live_ids, f"живого анонса нет: {h.screen_text()[:200]!r}")
        screen_id = h.screen_message_id()
        self.assertIsNotNone(screen_id, "экран вопроса ещё не создан")
        assert screen_id is not None
        self.assertLess(
            min(live_ids),
            int(screen_id),
            "живой анонс оказался ниже вопроса",
        )
        live_index = max(
            index for index, call in enumerate(h.calls) if is_live_record(call)
        )
        screen_index = max(
            index
            for index, call in enumerate(h.calls)
            if is_screen_record(call)
            and int(screen_id) in h.bot.call_message_ids.get(id(call), ())
        )
        self.assertLess(live_index, screen_index, "вопрос нарисован раньше анонса")
        self.assertNotIn(min(live_ids), h.deleted_ids(), "живой анонс удалён")

    @scenario_test
    async def test_invalid_input_keeps_message_and_explains_error(self, h) -> None:
        place_id = await _place(h)
        await _open_place_questions(h, place_id)
        await h.text("Читка пьесы")
        before = h.screen_text()
        self.assertEqual(_step_total(before), (2, 5), before)

        update = await h.text("не дата, а слово")
        user_message_id = update.effective_message.message_id
        screen = h.screen_text()

        self.assertNotIn(
            user_message_id,
            h.deleted_ids(),
            "сообщение пользователя удалено при ошибке ввода",
        )
        self.assertEqual(_step_total(screen), (2, 5), "шаг изменился после ошибки")
        cat.assert_shows(self, screen, "wizard.question.error")
        error_block = _blocks(screen)[-1]
        self.assertTrue(error_block.startswith("⚠️"), f"нет ошибки: {error_block!r}")
        self.assertIn(
            "дат",
            error_block.lower(),
            f"объяснение не конкретное: {error_block!r}",
        )
        # Подсказка осталась единственной: ошибка — отдельный блок ПОСЛЕ неё.
        self.assertEqual(len(_blocks(screen)), 3, f"состав экрана: {_blocks(screen)!r}")
        self.assertEqual(_blocks(screen)[1], _blocks(before)[1])

        # Успешный ввод переводит дальше без служебных сообщений.
        calls_before = len(h.calls)
        await h.text("09.12.2026")
        self.assertEqual(_step_total(h.screen_text()), (3, 5), h.screen_text())
        new_messages = [
            call
            for call in h.calls[calls_before:]
            if call["type"] in {"message", "photo", "media_group"}
        ]
        self.assertEqual(new_messages, [], "успешный ввод создал лишнее сообщение")


if __name__ == "__main__":
    unittest.main()
