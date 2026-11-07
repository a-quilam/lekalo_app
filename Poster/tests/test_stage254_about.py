# tests/test_stage254_about.py
"""
Poster 2.5.4 §7: Rich Message «О проекте» и переход со справки.

Проверяется пользовательский контракт, а не реализация:

1. на Rich Message справки «◀️ Назад» и «ℹ️ О проекте» стоят в ОДНОЙ
   строке; открытие «О проекте» не оставляет прежнюю справку живой;
2. «О проекте» — настоящий Rich Message (`sendRichMessage`, не
   `sendMessage`), способ `blocks`, заголовок и ровно шесть СВЁРНУТЫХ
   разделов: «Лекало», «Как всё начиналось», «Для кого», «Открытый код»,
   «Связаться», «Поддержать проект»;
3. ссылки кликабельные и с короткой подписью (`type: "url"`), GitHub — один
   и тот же адрес; упомянуты «РанДом Культуры» и автор проекта;
4. у «Поддержать проект» нет кнопки и ссылки на донаты, у Rich Message
   ровно одна кнопка — «◀️ К помощи»;
5. «◀️ К помощи» возвращает справку, а её «◀️ Назад» — на ТОТ ЖЕ
   экран-источник: раздел, редактор анонса и шаг конструктора не теряются;
6. состояние переживает рестарт между шагами, в чате не появляется двух
   конкурирующих клавиатур;
7. при отказе Telegram виден понятный экран с кнопкой «◀️ К помощи» —
   пользователь не остаётся без пути назад.

Сценарии идут через настоящее приложение (`bot.build_application`), FakeBot и
временную SQLite (`tests/integration_harness`). Реальная база, сеть Telegram
и настоящий бот не используются.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage254_about -v
"""

from __future__ import annotations

import unittest
from typing import Any

from tests import ui_texts_expect as cat
from utils.ui_texts import tr

try:
    from telegram.error import BadRequest

    from about_rich import (
        AUTHOR_URL,
        GITHUB_URL,
        RANDOM_CULTURE_URL,
        build_about_rich_message,
    )
    from handlers.main_menu import about_keyboard
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        inline_buttons,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        _choose_personal,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )
    from utils import screen as screen_utils

    IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    IMPORT_ERROR = str(exc)


BACK_LABEL = "◀️ Назад"
BACK_DATA = "help_back"
ABOUT_LABEL = "ℹ️ О проекте"
ABOUT_DATA = "help_about"
TO_HELP_LABEL = "◀️ К помощи"
TO_HELP_DATA = "about_help"
MENU_TEXT = tr("menu.main")
RICH_KEY = "help_rich_message_id"

# Названия разделов — из каталога: владелец правит YAML, проверка остаётся
# проверкой состава и порядка шести разделов.
SECTION_TITLES = tuple(
    cat.text(f"about.rich.section{number}") for number in range(1, 7)
)


# --- помощники -------------------------------------------------------------


def _rich_records(h: Harness) -> list[dict[str, Any]]:
    return list(h.bot.rich_records)


def _blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = payload["blocks"]
    assert isinstance(blocks, list)
    return blocks


def _urls(value: Any) -> list[dict[str, Any]]:
    """Все `RichTextUrl` внутри `RichText` (строка/массив/объект)."""
    if isinstance(value, dict):
        return [value] if value.get("type") == "url" else []
    if isinstance(value, list):
        return [url for item in value for url in _urls(item)]
    return []


def _texts(value: Any) -> str:
    """Плоский текст `RichText` (ссылка раскрывается в свою подпись)."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_texts(item) for item in value)
    if isinstance(value, dict):
        return _texts(value.get("text", ""))
    return ""


def _block_texts(payload: dict[str, Any]) -> str:
    """Весь читаемый текст сообщения (абзацы, разделы, элементы списков)."""
    parts: list[str] = []

    def walk(block: dict[str, Any]) -> None:
        if block.get("type") == "paragraph":
            parts.append(_texts(block.get("text", "")))
        elif block.get("type") == "details":
            parts.append(_texts(block.get("summary", "")))
        for child in block.get("blocks") or ():
            walk(child)
        for item in block.get("items") or ():
            for child in item.get("blocks") or ():
                walk(child)

    for block in _blocks(payload):
        walk(block)
    return "\n".join(parts)


def _all_urls(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(block: dict[str, Any]) -> None:
        found.extend(_urls(block.get("text", "")))
        found.extend(_urls(block.get("summary", "")))
        for child in block.get("blocks") or ():
            walk(child)
        for item in block.get("items") or ():
            for child in item.get("blocks") or ():
                walk(child)

    for block in _blocks(payload):
        walk(block)
    return found


def _live_keyboards(h: Harness) -> list[int]:
    """id живых сообщений бота с inline-клавиатурой (конкурирующие меню)."""
    dead = set(h.deleted_ids())
    return [
        int(message_id)
        for (_chat, message_id), entry in sorted(h.bot._message_content.items())
        if int(message_id) not in dead and entry[2] is not None
    ]


# —— 1. Структура «О проекте» ————————————————————————————————————————


class AboutStructureTests(unittest.TestCase):
    """§7: второй Rich Message собирается только из blocks официальной схемы."""

    def setUp(self) -> None:
        self.payload = build_about_rich_message().to_dict()
        self.blocks = _blocks(self.payload)

    def test_single_way_is_blocks(self) -> None:
        self.assertEqual(list(self.payload), ["blocks"])

    def test_heading_and_intro_come_from_catalog(self) -> None:
        self.assertEqual(self.blocks[0]["type"], "heading")
        self.assertEqual(self.blocks[0]["text"], tr("about.rich.title"))
        self.assertEqual(self.blocks[0]["size"], 1)
        texts = [b["text"] for b in self.blocks if b["type"] == "paragraph"]
        self.assertIn(tr("about.rich.intro"), texts)
        self.assertIn(tr("about.rich.hint"), texts)

    def test_exactly_six_collapsed_sections(self) -> None:
        details = [b for b in self.blocks if b["type"] == "details"]
        self.assertEqual([b["summary"] for b in details], list(SECTION_TITLES))
        for block in details:
            self.assertNotIn(
                "is_open",
                block,
                "свёрнутый раздел не должен передавать is_open (§7)",
            )
            self.assertTrue(block["blocks"], "раздел без содержимого")

    def test_sections_cover_required_topics(self) -> None:
        text = _block_texts(self.payload)
        self.assertIn("Лекало", text)
        self.assertIn("РанДом Культуры", text)
        self.assertIn("Поддержать проект", text)
        # «Поддержать проект» — без кнопки и без ссылки на донаты.
        support = next(
            b for b in self.blocks if b.get("summary") == "Поддержать проект"
        )
        support_text = _texts(support["blocks"])
        for forbidden in ("донат", "Донат", "перевести", "оплатить", "счёт"):
            self.assertNotIn(forbidden, support_text)
        self.assertEqual(_urls(support["blocks"]), [], "в поддержке есть ссылка")

    def test_links_are_clickable_with_short_labels(self) -> None:
        urls = _all_urls(self.payload)
        self.assertTrue(urls, "в «О проекте» нет ни одной ссылки")
        by_url = {item["url"]: item for item in urls}
        self.assertIn(GITHUB_URL, by_url)
        self.assertIn(AUTHOR_URL, by_url)
        self.assertIn(RANDOM_CULTURE_URL, by_url)
        for item in urls:
            # Официальная схема RichTextUrl: ровно три поля.
            self.assertEqual(set(item), {"type", "text", "url"})
            self.assertEqual(item["type"], "url")
            label = _texts(item["text"])
            self.assertTrue(label.strip(), "пустая подпись ссылки")
            self.assertNotIn("http", label, "подпись ссылки — сам адрес, а не текст")
            self.assertLess(len(label), 40, "подпись ссылки слишком длинная")
            self.assertTrue(item["url"].startswith("https://"))

    def test_github_url_is_the_expected_repository(self) -> None:
        self.assertEqual(GITHUB_URL, "https://github.com/a-quilam/samplePost_bot")

    def test_rich_message_itself_has_no_extra_buttons(self) -> None:
        """Rich Message не несёт клавиатуры: она передаётся отдельно (§7)."""
        self.assertEqual(list(build_about_rich_message().to_dict()), ["blocks"])


# —— 2. Кнопки справки и переход ————————————————————————————————————


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class HelpAboutButtonsTests(unittest.TestCase):
    """§7: «◀️ Назад | ℹ️ О проекте» — одна строка, без донат-кнопки."""

    @scenario_test
    async def test_help_puts_back_and_about_in_one_row(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        record = _rich_records(h)[0]

        buttons = inline_buttons(record["reply_markup"])
        self.assertEqual(buttons, [(BACK_LABEL, BACK_DATA), (ABOUT_LABEL, ABOUT_DATA)])
        rows = record["reply_markup"].inline_keyboard
        self.assertEqual(len(rows), 1, "кнопки справки должны стоять в одной строке")
        self.assertEqual(
            [button.callback_data for button in rows[0]],
            [BACK_DATA, ABOUT_DATA],
            "порядок кнопок: сначала возврат, затем «О проекте»",
        )

    @scenario_test
    async def test_about_opens_as_rich_message_with_single_button(
        self, h: Harness
    ) -> None:
        await h.text("/start")
        await h.callback("help")
        help_id = _rich_records(h)[0]["message_id"]

        await h.callback(ABOUT_DATA)

        records = _rich_records(h)
        self.assertEqual(len(records), 2, "«О проекте» — отдельное Rich Message")
        record = records[-1]
        self.assertEqual(record["chat_id"], h.chat_id)
        self.assertEqual(record["rich_message"]["blocks"][0]["type"], "heading")
        buttons = inline_buttons(record["reply_markup"])
        self.assertEqual(buttons, [(TO_HELP_LABEL, TO_HELP_DATA)])
        # Прежняя справка удалена: двух Rich Message в чате не остаётся.
        self.assertIn(help_id, h.deleted_ids())
        self.assertEqual(h.user_data()[RICH_KEY], record["message_id"])

    @scenario_test
    async def test_about_is_not_sent_as_plain_message(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        messages_before = [call for call in h.calls if call["type"] == "message"]
        await h.callback(ABOUT_DATA)
        messages_after = [
            call
            for call in h.calls
            if call["type"] == "message" and call not in messages_before
        ]
        self.assertEqual(
            messages_after, [], "«О проекте» ушло обычным sendMessage (§54)"
        )
        rich_calls = [call for call in h.calls if call["type"] == "rich_message"]
        self.assertEqual(len(rich_calls), 2)

    @scenario_test
    async def test_about_has_no_competing_keyboards(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        self.assertEqual(len(_live_keyboards(h)), 1)
        await h.callback(ABOUT_DATA)
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")


# —— 3. Возвраты: справка → источник ————————————————————————————————


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class AboutReturnTests(unittest.TestCase):
    """§7: «◀️ К помощи» не теряет источник справки."""

    @scenario_test
    async def test_back_to_help_then_back_to_menu(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        await h.callback(ABOUT_DATA)
        about_id = _rich_records(h)[-1]["message_id"]

        await h.callback(TO_HELP_DATA)

        records = _rich_records(h)
        self.assertEqual(len(records), 3, "справка открыта заново")
        self.assertIn(about_id, h.deleted_ids(), "«О проекте» не удалён")
        self.assertEqual(
            inline_buttons(records[-1]["reply_markup"]),
            [(BACK_LABEL, BACK_DATA), (ABOUT_LABEL, ABOUT_DATA)],
            "после возврата кнопки справки прежние",
        )

        await h.callback(BACK_DATA)

        self.assertIn(MENU_TEXT, h.screen_text(), "вернулись не в меню")
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")
        self.assertNotIn(RICH_KEY, h.user_data())

    @scenario_test
    async def test_back_from_about_returns_to_section(self, h: Harness) -> None:
        """Источник справки — раздел «Места»: он и восстанавливается."""
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
        self.assertNotIn(MENU_TEXT, h.screen_text())

    @scenario_test
    async def test_about_from_constructor_keeps_resume_snapshot(
        self, h: Harness
    ) -> None:
        """Черновик и текущий шаг переживают справку и «О проекте»."""
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        question_text = h.screen_text()
        payload_before = screen_utils.load_payload(h.user_id) or {}

        await h.callback("help")
        await h.callback(ABOUT_DATA)
        await h.callback(TO_HELP_DATA)
        await h.callback(BACK_DATA)

        self.assertEqual(h.screen_text(), question_text, "шаг конструктора потерян")
        payload_after = screen_utils.load_payload(h.user_id) or {}
        self.assertEqual(
            set(payload_after), set(payload_before), "снимок конструктора изменился"
        )
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")

    @scenario_test
    async def test_about_survives_restart_and_returns_to_help(self, h: Harness) -> None:
        """Рестарт между шагами: id «О проекте» есть в БД, возврат работает."""
        await h.text("/start")
        await h.callback("help")
        await h.callback(ABOUT_DATA)
        about_id = _rich_records(h)[-1]["message_id"]

        await h.restart()

        self.assertEqual(screen_utils.load_rich_temp(h.user_id), about_id)
        await h.callback(TO_HELP_DATA)

        self.assertIn(about_id, h.deleted_ids(), "«О проекте» не удалён после рестарта")
        self.assertEqual(
            len(_rich_records(h)), 3, "справка не открылась после рестарта"
        )
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")

    @scenario_test
    async def test_about_closed_by_any_other_screen(self, h: Harness) -> None:
        """Уход в обычный экран удаляет временный Rich Message «О проекте»."""
        await h.text("/start")
        await h.callback("help")
        await h.callback(ABOUT_DATA)
        about_id = _rich_records(h)[-1]["message_id"]

        await h.callback(BACK_DATA)  # «Назад» уже со справки: фолбэк в меню
        self.assertIn(MENU_TEXT, h.screen_text())
        self.assertIn(about_id, h.deleted_ids())


# —— 4. Отказ Telegram ————————————————————————————————————————————————


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class AboutFailureTests(unittest.TestCase):
    """§7: отказ Telegram — понятная плашка и путь назад к справке."""

    @scenario_test
    async def test_about_failure_shows_notice_with_way_back(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        help_id = _rich_records(h)[0]["message_id"]
        h.bot.errors_by_type["rich_message"] = BadRequest("Unsupported rich message")

        await h.callback(ABOUT_DATA)

        self.assertEqual(len(_rich_records(h)), 1, "сбой не должен считаться отправкой")
        notice = tr("about.rich.unavailable").split(".")[0]
        self.assertIn(notice, h.screen_text(), "нет понятной плашки сбоя")
        self.assertEqual(
            h.screen_buttons(),
            inline_buttons(about_keyboard()),
            "нет кнопки возврата к справке",
        )
        self.assertIn(help_id, h.deleted_ids(), "прежняя справка осталась в чате")
        self.assertEqual(len(_live_keyboards(h)), 1, "две конкурирующие клавиатуры")

    @scenario_test
    async def test_back_to_help_works_after_about_failure(self, h: Harness) -> None:
        await h.text("/start")
        await h.callback("help")
        h.bot.errors_by_type["rich_message"] = BadRequest("Unsupported rich message")
        await h.callback(ABOUT_DATA)
        self.assertIn(TO_HELP_LABEL, [label for label, _ in h.screen_buttons()])

        # Telegram снова доступен: «◀️ К помощи» открывает справку.
        h.bot.errors_by_type.clear()
        await h.callback(TO_HELP_DATA)

        self.assertEqual(len(_rich_records(h)), 2, "справка не открылась")
        self.assertEqual(
            inline_buttons(_rich_records(h)[-1]["reply_markup"]),
            [(BACK_LABEL, BACK_DATA), (ABOUT_LABEL, ABOUT_DATA)],
        )

    @scenario_test
    async def test_help_failure_still_shows_menu(self, h: Harness) -> None:
        """Сбой справки — прежний контракт 2.5.1 не ослаблен."""
        h.bot.errors_by_type["rich_message"] = BadRequest("can't parse entities")
        await h.text("/start")
        await h.callback("help")

        self.assertEqual(_rich_records(h), [])
        notice = tr("help.rich.unavailable").split(".")[0]
        self.assertIn(notice, h.screen_text())
        self.assertNotIn(ABOUT_DATA, [data for _label, data in h.screen_buttons()])


if __name__ == "__main__":
    unittest.main()
