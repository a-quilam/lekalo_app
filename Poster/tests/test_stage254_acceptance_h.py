# tests/test_stage254_acceptance_h.py
r"""
Poster 2.5.4 §7 + §9 (тест H) — НЕЗАВИСИМАЯ приёмка «Rich Message О проекте».

Проверяется пользовательский контракт, а не реализация. Ожидания сняты до
чтения кода и записаны здесь как требования владельца:

1. на Rich Message справки «◀️ Назад» и «ℹ️ О проекте» стоят в ОДНОЙ строке;
2. «О проекте» — именно Rich Message (`sendRichMessage`, способ `blocks`),
   а не обычный `sendMessage` с текстом: заголовок, вводный абзац, подсказка,
   разделитель и ШЕСТЬ свёрнутых разделов; в разделах есть абзацы и список;
3. ссылки — кликабельные объекты `type: "url"` с короткой подписью, а не
   «голый» текст адреса: GitHub, автор проекта, «РанДом Культуры»;
4. у «Поддержать проект» нет донат-кнопки/донат-ссылки, у сообщения ровно
   одна кнопка — «◀️ К помощи»;
5. «К помощи» возвращает справку, «Назад» на ней — на ТОТ ЖЕ экран-источник
   (меню, раздел, текущий шаг черновика); исходный экран/черновик не теряется;
6. в чате не остаётся двух конкурирующих клавиатур: прежний Rich Message
   удаляется при каждом переходе, повторные (устаревшие) нажатия не плодят
   живые клавиатуры;
7. отказ Telegram на «О проекте» даёт понятный экран с кнопкой «К помощи»,
   после снятия сбоя возврат к справке и в исходный контекст работает;
8. рестарт службы между шагами не ломает переходы: id Rich Message живёт в
   БД и старое сообщение удаляется; черновик и данные разделов целы. Возврат
   «Назад» после рестарта идёт в главное меню — это действующий контракт
   2.5.1 (`help_return` живёт только в памяти, `tests/test_stage251_help_return`),
   а не дефект H.

ОТДЕЛЬНО, ОЖИДАЕМО ПАДАЮЩИЙ ТЕСТ (DEFECT F1, решение менеджера):

    `/help` ИЗ КОНСТРУКТОРА снимает resume-снимок шага: команда идёт через
    `_command_to_section`, который вызывает `_clear_resume`, «Назад» из
    справки уводит в главное меню, текущий шаг черновика теряется. Контракт
    2.5.1 «/help — не выход» (закреплён
    `tests/test_poster2_h1_stability_formatting.py::ExplicitExitAfterRestartTests`)
    требует обратного: справка не выход, «Назад» ведёт на тот же шаг.
    Тест `F1HelpCommandKeepsWizardStepTests` помечен EXPECTED FAILURE —
    он воспроизводит дефект и должен упасть до исправления Developer'ом.

Сценарии идут через настоящее приложение (`bot.build_application`), FakeBot и
временную SQLite (`tests/integration_harness`): реальная `post_bot.db`,
`.env` и сеть Telegram не используются.

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage254_acceptance_h -v
"""

from __future__ import annotations

import unittest
from typing import Any

try:
    from telegram.error import BadRequest

    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        inline_buttons,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        _choose_personal,
        _latest_draft,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )
    from utils import screen as screen_utils
    from utils.ui_texts import DEFAULTS

    IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    IMPORT_ERROR = str(exc)


# —— ожидания контракта (свои, не из реализации) ———————————————————————
BACK_LABEL = "◀️ Назад"
BACK_DATA = "help_back"
ABOUT_LABEL = "ℹ️ О проекте"
ABOUT_DATA = "help_about"
TO_HELP_LABEL = "◀️ К помощи"
TO_HELP_DATA = "about_help"
MENU_TEXT = "Что хотите сделать?"
QUESTION_TEXT = "Вопрос 1 из 1"

ABOUT_TITLE = "О проекте"
SECTION_TITLES = (
    "Лекало",
    "Как всё начиналось",
    "Для кого",
    "Открытый код",
    "Связаться",
    "Поддержать проект",
)
GITHUB_URL = "https://github.com/a-quilam/samplePost_bot"
AUTHOR_URL = "https://t.me/a_quilam"
RANDOM_CULTURE_URL = "https://t.me/random_culture"
EXPECTED_URLS = {GITHUB_URL, AUTHOR_URL, RANDOM_CULTURE_URL}
RESUME_PAYLOAD_KEY = "announcement_resume"
RICH_TEMP_PAYLOAD_KEY = "rich_temp"


# --- помощники -------------------------------------------------------------


def _rich(h: Harness) -> list[dict[str, Any]]:
    return list(h.bot.rich_records)


def _blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = payload["blocks"]
    assert isinstance(blocks, list)
    return blocks


def _texts(value: Any) -> str:
    """Плоский текст `RichText` (ссылка раскрывается в свою подпись)."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_texts(item) for item in value)
    if isinstance(value, dict):
        return _texts(value.get("text", ""))
    return ""


def _urls(value: Any) -> list[dict[str, Any]]:
    """Все объекты `type: "url"` внутри `RichText`/блока."""
    if isinstance(value, dict):
        if value.get("type") == "url":
            return [value]
        found: list[dict[str, Any]] = []
        for child in value.get("blocks") or ():
            found.extend(_urls(child))
        for item in value.get("items") or ():
            found.extend(_urls(item))
        return found
    if isinstance(value, list):
        return [url for item in value for url in _urls(item)]
    return []


def _block_urls(block: dict[str, Any]) -> list[dict[str, Any]]:
    found = _urls(block.get("text", ""))
    found.extend(_urls(block.get("summary", "")))
    for child in block.get("blocks") or ():
        found.extend(_block_urls(child))
    for item in block.get("items") or ():
        for child in item.get("blocks") or ():
            found.extend(_block_urls(child))
    return found


def _block_texts(block: dict[str, Any]) -> str:
    """Весь читаемый текст одного блока (абзацы, разделы, элементы списка)."""
    parts: list[str] = []
    if block.get("type") == "paragraph":
        parts.append(_texts(block.get("text", "")))
    elif block.get("type") == "details":
        parts.append(_texts(block.get("summary", "")))
    for child in block.get("blocks") or ():
        parts.append(_block_texts(child))
    for item in block.get("items") or ():
        for child in item.get("blocks") or ():
            parts.append(_block_texts(child))
    return "\n".join(parts)


def _live_keyboards(h: Harness) -> list[int]:
    """id ЖИВЫХ сообщений бота с inline-клавиатурой (конкурирующие меню)."""
    dead = {int(mid) for mid in h.deleted_ids()}
    out: list[int] = []
    for (_chat_id, message_id), entry in sorted(h.bot._message_content.items()):
        if int(message_id) in dead:
            continue
        if entry[2] is not None:
            out.append(int(message_id))
    return out


def _created(h: Harness, since: int) -> list[str]:
    """
    Виды вызовов, создавших НОВОЕ сообщение, начиная с индекса `since`.

    Нужно, чтобы отделить Rich Message от обычного `sendMessage`: подмена
    структурированного сообщения текстом запрещена (§7/§54).
    """
    kinds = {"message", "photo", "media_group", "rich_message"}
    return [call["type"] for call in h.calls[since:] if call["type"] in kinds]


def _payload(h: Harness) -> dict[str, Any]:
    return screen_utils.load_payload(h.user_id) or {}


async def _open_question(h: Harness):
    """Конструктор на первом вопросе личного шаблона (одно поле)."""
    await h.text("/start")
    place_id, _alias_id = _seed_place(h)
    template_id, _revision, bricks = _seed_personal_template(h)
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    return _latest_draft(h), bricks


# —— 1. Структура «О проекте» как Rich Message ———————————————————————


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class AboutStructureTests(unittest.TestCase):
    """§7: второй Rich Message — официальная схема `blocks`, не простой текст."""

    @scenario_test
    async def test_about_sent_as_rich_message_not_plain_text(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        before = len(h.calls)

        await h.callback(ABOUT_DATA)

        self.assertEqual(
            _created(h, before),
            ["rich_message"],
            "«О проекте» — ровно одно новое сообщение и это sendRichMessage",
        )
        record = _rich(h)[-1]
        payload = record["rich_message"]
        self.assertEqual(
            list(payload),
            ["blocks"],
            "Rich Message передаётся способом blocks (ровно один способ)",
        )
        blocks = _blocks(payload)
        self.assertEqual(
            len(blocks),
            10,
            "структура: заголовок, вступление, подсказка, разделитель, 6 разделов",
        )
        self.assertEqual(
            [block["type"] for block in blocks],
            [
                "heading",
                "paragraph",
                "paragraph",
                "divider",
                "details",
                "details",
                "details",
                "details",
                "details",
                "details",
            ],
        )

    @scenario_test
    async def test_heading_intro_and_hint(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        await h.callback(ABOUT_DATA)
        blocks = _blocks(_rich(h)[-1]["rich_message"])

        heading = blocks[0]
        self.assertEqual(heading["type"], "heading")
        self.assertEqual(heading["text"], ABOUT_TITLE)
        self.assertEqual(heading["size"], 1, "заголовок — самый крупный по схеме")

        intro, hint = blocks[1], blocks[2]
        self.assertEqual(intro["type"], "paragraph")
        self.assertEqual(hint["type"], "paragraph")
        self.assertTrue(_texts(intro["text"]).strip(), "пустой вводный абзац")
        self.assertTrue(_texts(hint["text"]).strip(), "пустая подсказка")
        self.assertEqual(blocks[3], {"type": "divider"})

    @scenario_test
    async def test_six_collapsed_sections_with_content(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        await h.callback(ABOUT_DATA)
        blocks = _blocks(_rich(h)[-1]["rich_message"])
        details = [block for block in blocks if block["type"] == "details"]

        self.assertEqual(
            [block["summary"] for block in details],
            list(SECTION_TITLES),
            "шесть разделов в заданном порядке",
        )
        for block in details:
            self.assertNotIn(
                "is_open",
                block,
                "свёрнутый раздел не передаёт is_open (отсутствие = свёрнуто)",
            )
            self.assertTrue(block.get("blocks"), "раздел без содержимого")

    @scenario_test
    async def test_sections_have_paragraphs_and_list(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        await h.callback(ABOUT_DATA)
        blocks = _blocks(_rich(h)[-1]["rich_message"])
        by_summary = {
            block["summary"]: block for block in blocks if block["type"] == "details"
        }

        for title in SECTION_TITLES:
            kinds = [child["type"] for child in by_summary[title]["blocks"]]
            self.assertIn("paragraph", kinds, f"раздел «{title}» без абзаца")

        for_whom = by_summary["Для кого"]
        lists = [child for child in for_whom["blocks"] if child["type"] == "list"]
        self.assertEqual(len(lists), 1, "«Для кого» — один список")
        items = lists[0]["items"]
        self.assertEqual(len(items), 3, "в списке «Для кого» три пункта")
        for item in items:
            self.assertEqual(list(item), ["blocks"], "пункт — объект со своими blocks")
            nested = item["blocks"]
            self.assertEqual(nested[0]["type"], "paragraph")
            self.assertTrue(_texts(nested[0]["text"]).strip())

    @scenario_test
    async def test_links_are_url_objects_with_short_labels(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        await h.callback(ABOUT_DATA)
        blocks = _blocks(_rich(h)[-1]["rich_message"])

        urls = [url for block in blocks for url in _block_urls(block)]
        self.assertEqual(
            len(urls),
            3,
            "ровно три ссылки: GitHub, автор проекта, «РанДом Культуры»",
        )
        self.assertEqual({item["url"] for item in urls}, EXPECTED_URLS)
        for item in urls:
            self.assertEqual(
                set(item),
                {"type", "text", "url"},
                "официальная схема RichTextUrl: type/text/url",
            )
            self.assertEqual(item["type"], "url")
            label = _texts(item["text"])
            self.assertTrue(label.strip(), "пустая подпись ссылки")
            self.assertNotIn("https://", label, "подпись — адрес, а не текст")
            self.assertLess(len(label), 40, "подпись ссылки слишком длинная")

    @scenario_test
    async def test_no_donation_button_or_link(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        await h.callback(ABOUT_DATA)
        record = _rich(h)[-1]
        blocks = _blocks(record["rich_message"])

        support = next(
            block for block in blocks if block.get("summary") == "Поддержать проект"
        )
        text = _block_texts(support).lower()
        for forbidden in ("донат", "перевести", "оплатить", "реквизит"):
            self.assertNotIn(forbidden, text, "в поддержке упомянуты донаты")
        self.assertEqual(_block_urls(support), [], "в поддержке есть ссылка")

        buttons = inline_buttons(record["reply_markup"])
        for label, data in buttons:
            self.assertNotIn("донат", label.lower())
            self.assertNotIn("donate", (data or "").lower())
        self.assertEqual(
            buttons,
            [(TO_HELP_LABEL, TO_HELP_DATA)],
            "у «О проекте» ровно одна кнопка — возврат к справке",
        )


# —— 2. Клавиатуры справки и «О проекте» ——————————————————————————————


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class KeyboardTests(unittest.TestCase):
    """§7: «◀️ Назад | ℹ️ О проекте» — одна строка; у «О проекте» одна кнопка."""

    @scenario_test
    async def test_help_buttons_are_in_one_row(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        record = _rich(h)[-1]

        rows = record["reply_markup"].inline_keyboard
        self.assertEqual(len(rows), 1, "кнопки справки стоят не в одной строке")
        self.assertEqual(
            [(button.text, button.callback_data) for button in rows[0]],
            [(BACK_LABEL, BACK_DATA), (ABOUT_LABEL, ABOUT_DATA)],
            "порядок: сначала возврат, затем «О проекте»",
        )

    @scenario_test
    async def test_about_keyboard_has_single_back_button(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        await h.callback(ABOUT_DATA)

        record = _rich(h)[-1]
        rows = record["reply_markup"].inline_keyboard
        self.assertEqual(len(rows), 1, "лишняя строка кнопок у «О проекте»")
        self.assertEqual(
            [(button.text, button.callback_data) for button in rows[0]],
            [(TO_HELP_LABEL, TO_HELP_DATA)],
        )


# —— 3. Переходы и конкурентные клавиатуры ——————————————————————————


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class NavigationTests(unittest.TestCase):
    """§7: справка ↔ «О проекте» ↔ источник, без двух живых клавиатур."""

    @scenario_test
    async def test_about_replaces_help_message(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        help_id = _rich(h)[0]["message_id"]
        before = len(h.calls)

        await h.callback(ABOUT_DATA)

        self.assertEqual(_created(h, before), ["rich_message"])
        self.assertIn(help_id, h.deleted_ids(), "справка осталась живой")
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")
        self.assertEqual(
            _live_keyboards(h), [_rich(h)[-1]["message_id"]], "живая не та клавиатура"
        )

    @scenario_test
    async def test_to_help_restores_help(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        await h.callback(ABOUT_DATA)
        about_id = _rich(h)[-1]["message_id"]

        await h.callback(TO_HELP_DATA)

        records = _rich(h)
        self.assertEqual(len(records), 3, "справка не отправлена заново")
        self.assertIn(about_id, h.deleted_ids(), "«О проекте» остался живым")
        self.assertEqual(
            inline_buttons(records[-1]["reply_markup"]),
            [(BACK_LABEL, BACK_DATA), (ABOUT_LABEL, ABOUT_DATA)],
            "у восстановленной справки не прежние кнопки",
        )
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")

    @scenario_test
    async def test_menu_route_returns_to_menu(self, h: Harness) -> None:
        """§9-H п.10: меню → справка → о проекте → к помощи → назад → меню."""
        await h.text("/start")
        menu_buttons = h.screen_buttons()

        await h.callback("help")
        await h.callback(ABOUT_DATA)
        await h.callback(TO_HELP_DATA)
        await h.callback(BACK_DATA)

        self.assertIn(MENU_TEXT, h.screen_text(), "вернулись не в меню")
        self.assertEqual(h.screen_buttons(), menu_buttons, "клавиатура меню другая")
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")

    @scenario_test
    async def test_section_source_is_restored(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("manage")
        await h.callback("manage_templates")
        section_text = h.screen_text()

        await h.callback("help")
        await h.callback(ABOUT_DATA)
        await h.callback(TO_HELP_DATA)
        await h.callback(BACK_DATA)

        self.assertEqual(
            h.screen_text(), section_text, "раздел-источник не восстановлен"
        )
        self.assertNotIn(MENU_TEXT, h.screen_text(), "вместо раздела открылось меню")
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")

    @scenario_test
    async def test_constructor_step_and_draft_are_restored(self, h: Harness) -> None:
        draft, _bricks = await _open_question(h)
        question_text = h.screen_text()
        question_buttons = h.screen_buttons()
        payload_before = _payload(h)

        await h.callback("help")
        await h.callback(ABOUT_DATA)
        await h.callback(TO_HELP_DATA)
        await h.callback(BACK_DATA)

        self.assertEqual(h.screen_text(), question_text, "шаг черновика потерян")
        self.assertEqual(h.screen_buttons(), question_buttons, "кнопки шага другие")
        self.assertEqual(_latest_draft(h).id, draft.id, "черновик подменён")
        self.assertEqual(
            set(_payload(h)), set(payload_before), "resume-снимок конструктора изменён"
        )
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")

    @scenario_test
    async def test_repeated_stale_presses_do_not_stack_keyboards(
        self, h: Harness
    ) -> None:
        """Повторное нажатие по уже удалённой клавиатуре — не второй экран."""
        await h.text("/start")
        await h.callback("help")
        await h.callback(ABOUT_DATA)
        await h.callback(ABOUT_DATA)  # устаревшее: справки уже нет

        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")
        self.assertEqual(
            inline_buttons(_rich(h)[-1]["reply_markup"]),
            [(TO_HELP_LABEL, TO_HELP_DATA)],
            "после повторного нажатия открылся не «О проекте»",
        )

        await h.callback(TO_HELP_DATA)
        await h.callback(TO_HELP_DATA)  # устаревшее: «О проекте» уже нет

        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")
        self.assertEqual(
            inline_buttons(_rich(h)[-1]["reply_markup"]),
            [(BACK_LABEL, BACK_DATA), (ABOUT_LABEL, ABOUT_DATA)],
            "после повторного нажатия открылась не справка",
        )

        await h.callback(BACK_DATA)
        self.assertIn(MENU_TEXT, h.screen_text())
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")


# —— 4. Рестарт между шагами —————————————————————————————————————————


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class RestartTests(unittest.TestCase):
    """§9-H п.7: id Rich Message переживают рестарт, переходы работают."""

    @scenario_test
    async def test_restart_between_help_and_about(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        help_id = _rich(h)[0]["message_id"]

        await h.restart()

        self.assertEqual(
            screen_utils.load_rich_temp(h.user_id),
            help_id,
            "id справки не сохранён в БД — после рестарта её нечем удалить",
        )
        await h.callback(ABOUT_DATA)
        self.assertIn(help_id, h.deleted_ids(), "справка не удалена после рестарта")
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")

        await h.callback(TO_HELP_DATA)
        await h.callback(BACK_DATA)
        self.assertIn(MENU_TEXT, h.screen_text(), "возврат в меню не работает")

    @scenario_test
    async def test_restart_while_about_open_returns_to_help(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        await h.callback(ABOUT_DATA)
        about_id = _rich(h)[-1]["message_id"]

        await h.restart()

        self.assertEqual(screen_utils.load_rich_temp(h.user_id), about_id)
        await h.callback(TO_HELP_DATA)

        self.assertIn(about_id, h.deleted_ids(), "«О проекте» не удалён")
        self.assertEqual(
            inline_buttons(_rich(h)[-1]["reply_markup"]),
            [(BACK_LABEL, BACK_DATA), (ABOUT_LABEL, ABOUT_DATA)],
        )
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")

    @scenario_test
    async def test_restart_does_not_lose_draft_or_section(self, h: Harness) -> None:
        draft, _bricks = await _open_question(h)
        await h.callback("help")
        await h.callback(ABOUT_DATA)

        await h.restart()
        payload = _payload(h)
        self.assertIn(
            RESUME_PAYLOAD_KEY,
            payload,
            "рестарт/справка сняли resume-снимок конструктора",
        )
        self.assertEqual(_latest_draft(h).id, draft.id, "черновик потерян")
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")


# —— 5. Отказ Telegram ————————————————————————————————————————————


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class FailureTests(unittest.TestCase):
    """§9-H п.9: сбой «О проекте» — понятный экран, путь назад, восстановление."""

    @scenario_test
    async def test_about_failure_shows_notice_with_way_back(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        help_id = _rich(h)[0]["message_id"]
        h.bot.errors_by_type["rich_message"] = BadRequest("Unsupported rich message")

        await h.callback(ABOUT_DATA)

        self.assertEqual(len(_rich(h)), 1, "сбой не должен считаться отправкой")
        notice = DEFAULTS["about.rich.unavailable"].split(":")[0]
        self.assertIn(notice, h.screen_text(), "нет понятного сообщения о сбое")
        self.assertEqual(
            h.screen_buttons(),
            [(TO_HELP_LABEL, TO_HELP_DATA)],
            "на экране сбоя нет пути назад к справке",
        )
        self.assertIn(help_id, h.deleted_ids(), "прежняя справка осталась в чате")
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")

    @scenario_test
    async def test_about_failure_keeps_source_context_and_recovers(
        self, h: Harness
    ) -> None:
        draft, _bricks = await _open_question(h)
        question_text = h.screen_text()
        await h.callback("help")
        h.bot.errors_by_type["rich_message"] = BadRequest("Unsupported rich message")
        await h.callback(ABOUT_DATA)
        self.assertIn(TO_HELP_LABEL, [label for label, _data in h.screen_buttons()])

        # Telegram снова доступен: «К помощи» открывает справку, «Назад» —
        # ровно на тот шаг, откуда пользователь уходил в справку.
        h.bot.errors_by_type.pop("rich_message", None)
        await h.callback(TO_HELP_DATA)
        self.assertEqual(
            inline_buttons(_rich(h)[-1]["reply_markup"]),
            [(BACK_LABEL, BACK_DATA), (ABOUT_LABEL, ABOUT_DATA)],
        )
        await h.callback(BACK_DATA)

        self.assertEqual(
            h.screen_text(), question_text, "после сбоя потерян шаг черновика"
        )
        self.assertEqual(_latest_draft(h).id, draft.id, "черновик подменён")
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")


# —— 6. ДЕФЕКТ F1: `/help` из конструктора ————————————————————————————


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class F1HelpCommandKeepsWizardStepTests(unittest.TestCase):
    """
    DEFECT F1 — ОЖИДАЕМО ПАДАЮЩИЙ ТЕСТ (ожидание менеджера).

    Контракт 2.5.1: `/help` — НЕ выход из конструктора. Справка не снимает
    resume-снимок шага, а «◀️ Назад» возвращает на тот же шаг черновика
    (так же, как кнопка «Помощь» и как `/help` после рестарта).

    Фактическое поведение a7348ee: команда `/help` идёт через
    `_command_to_section`, который вызывает `_clear_resume`; снимок шага
    стирается, источник справки определяется как «main», и «Назад» уводит
    в главное меню — пользователь теряет текущий шаг (данные черновика
    целы, но продолжить ввод можно только заново открыв анонс).

    Воспроизведение (без рестарта): конструктор → вопрос → `/help` →
    «◀️ Назад» → ожидается тот же вопрос, фактически главное меню.
    """

    @scenario_test
    async def test_help_command_keeps_resume_snapshot(self, h: Harness) -> None:
        await _open_question(h)
        self.assertIn(QUESTION_TEXT, h.screen_text())

        await h.text("/help")

        self.assertIn(
            RESUME_PAYLOAD_KEY,
            _payload(h),
            "EXPECTED FAILURE — DEFECT F1: `/help` снял resume-снимок шага "
            "черновика (контракт 2.5.1: справка — не выход)",
        )

    @scenario_test
    async def test_help_command_back_returns_to_wizard_step(self, h: Harness) -> None:
        draft, _bricks = await _open_question(h)
        question_text = h.screen_text()
        question_buttons = h.screen_buttons()

        await h.text("/help")
        await h.callback(BACK_DATA)

        self.assertEqual(
            h.screen_text(),
            question_text,
            "EXPECTED FAILURE — DEFECT F1: «Назад» из `/help` увёл в главное "
            "меню вместо текущего шага черновика",
        )
        self.assertEqual(h.screen_buttons(), question_buttons)
        self.assertEqual(_latest_draft(h).id, draft.id)
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")


if __name__ == "__main__":
    unittest.main()
