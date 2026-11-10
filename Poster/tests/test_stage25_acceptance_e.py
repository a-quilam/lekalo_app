# tests/test_stage25_acceptance_e.py
"""
Poster 2.5, независимая приёмка этапа E (Rich Message — справка) — Tester.

Проверяется утверждённый пользовательский контракт (spec §46–54, §60–61), а
не реализация Developer. Сценарии идут от того, что видит автор в чате:
какое сообщение появилось, какая у него клавиатура, что происходит при
возврате, что остаётся в чате после сбоя Telegram.

Что закрепляется:

1. §46–50 — `/help` и кнопка «❓ Помощь» дают РОВНО ОДИН настоящий
   `sendRichMessage`; аргумент — валидный `InputRichMessage` способом
   `blocks` (ровно один способ), обычного `sendMessage` с текстом справки нет.
2. §51 — заголовок «Poster — помощь», введение из спецификации и ровно
   десять свёрнутых (`is_open` не передаётся вовсе) раскрываемых разделов
   с нужными названиями; текст «Условных частей» объясняет скрытие тире и
   строки цены без программных выражений; раздел «Команды» сверяется с
   ФАКТИЧЕСКИ зарегистрированными `CommandHandler` (своя копия списка не
   используется).
3. §53 — кнопка возврата «🏠 В меню» с `callback_data=main_menu`; возврат
   восстанавливает меню, удаляет временный Rich Message, не оставляет двух
   клавиатур и не трогает доставленный анонс.
4. §54 — другие разделы (`Управление`, площадки, шаблоны, кирпичики, «Мои
   анонсы», создание анонса) Rich Message не используют.
5. §54 — отказ Telegram при отправке Rich: понятная реакция без исключения
   наружу и без подмены справки обычным `sendMessage`.
6. §61 — журнал операций FakeBot различает `send_message` и
   `sendRichMessage` (запись `rich_message` в общем журнале и отдельный
   `rich_records`).

Реальная `post_bot.db`, `.env` и сеть Telegram не используются: настоящий
`Application.process_update` поверх временной SQLite (`Harness`).

Ограничение, которое НЕ проверяется автоматически: реальное отображение
Rich Message в клиенте Telegram (свёрнутые блоки, вёрстка, раскрытие по
тапу) — FakeBot лишь фиксирует структуру запроса.

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_stage25_acceptance_e -v
"""

from __future__ import annotations

import re
import unittest

from telegram import InputFile
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.ext import CommandHandler, ConversationHandler

from tests import ui_texts_expect as cat

try:
    from help_rich import build_help_rich_message
    from rich_message import _hoist_input_files, validate_rich_message
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        inline_buttons,
        scenario_test,
    )
    from utils.rich_content import load_catalog

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


# §13: заголовок, подсказка и порядок девяти разделов — из Rich-каталога
# (`config/rich_content.yaml`, единственный источник утверждённого текста).
HELP_PAGE = load_catalog().help
HELP_TITLE = HELP_PAGE.title
SECTION_TITLES = tuple(section.title for section in HELP_PAGE.sections)

# §53 + Poster 2.5.1: кнопка возврата из справки на экран-источник.
BACK_LABEL = "◀️ Назад"
BACK_DATA = "help_back"
# Poster 2.5.4 §7: «ℹ️ О проекте» — вторая кнопка в ТОЙ ЖЕ строке справки.
ABOUT_LABEL = "ℹ️ О проекте"
ABOUT_DATA = "help_about"
# Прежняя кнопка «🏠 В меню» с Rich Message убрана (2.5.1).
LEGACY_BACK_DATA = "main_menu"

# Главное меню (§10): четыре постоянные кнопки.
MENU_BUTTONS = [
    ("📝 Создать анонс", "pick_place"),
    ("📂 Мои анонсы", "my_announcements"),
    ("⚙️ Управление", "manage"),
    ("❓ Помощь", "help"),
]

# §13.9: раздел, где перечислены ошибки и команда `/report`.
ERRORS_SECTION = "Ошибки и предложения"

RICH_KEY = "help_rich_message_id"


# --- помощники -------------------------------------------------------------


def _rich_records(h) -> list[dict]:
    return list(h.bot.rich_records)


def _blocks_of(record: dict) -> list[dict]:
    blocks = record["rich_message"]["blocks"]
    assert isinstance(blocks, list), f"blocks не список: {type(blocks)}"
    return blocks


def _details_of(record: dict) -> list[dict]:
    return [block for block in _blocks_of(record) if block.get("type") == "details"]


def _block_text(block: dict) -> str:
    """Весь текст одного блока (details — вместе с вложенными)."""
    kind = block.get("type")
    if kind == "heading":
        return str(block.get("text") or "")
    if kind == "paragraph":
        return str(block.get("text") or "")
    if kind == "list":
        # items — объекты InputRichBlockListItem со своим blocks (схема 10.1).
        return "\n".join(
            _block_text(child)
            for item in block.get("items") or ()
            for child in (item or {}).get("blocks") or ()
        )
    if kind == "details":
        parts = [str(block.get("summary") or "")]
        parts.extend(_block_text(child) for child in block.get("blocks") or ())
        return "\n".join(parts)
    return ""


def _rich_plain_text(record: dict) -> str:
    return "\n".join(_block_text(block) for block in _blocks_of(record))


def _section_text(record: dict, title: str) -> str:
    for block in _details_of(record):
        if block.get("summary") == title:
            return _block_text(block)
    raise AssertionError(
        f"нет раздела {title!r}; есть: {[b.get('summary') for b in _details_of(record)]}"
    )


def _live_messages(h) -> list[tuple[int, str, bool]]:
    """Живые сообщения бота: (message_id, вид, есть ли inline-клавиатура)."""
    dead = set(h.deleted_ids())
    return [
        (int(message_id), entry[0], entry[2] is not None)
        for (_chat, message_id), entry in sorted(h.bot._message_content.items())
        if int(message_id) not in dead
    ]


def _live_keyboards(h) -> list[int]:
    """id живых сообщений, у которых осталась inline-клавиатура."""
    return [
        message_id for message_id, _kind, has_markup in _live_messages(h) if has_markup
    ]


def _registered_commands(h) -> set[str]:
    """
    Команды, реально зарегистрированные в приложении.

    Источник истины — собранный `Application`: обычные `CommandHandler` в
    группах и обработчики внутри `ConversationHandler` (entry_points,
    состояния, fallbacks). Список не копируется из реализации справки.
    """
    found: set[str] = set()

    def add(handlers) -> None:
        for handler in handlers or ():
            if isinstance(handler, CommandHandler):
                # PTB хранит команды без слэша — приводим к виду справки.
                found.update(f"/{command}" for command in handler.commands)
            if isinstance(handler, ConversationHandler):
                add(handler.entry_points)
                add(handler.fallbacks)
                for state_handlers in (handler.states or {}).values():
                    add(state_handlers)

    for group in h.app.handlers.values():
        add(group)
    return found


def _commands_in_help(record: dict) -> set[str]:
    """Команды `/xxx`, упомянутые в разделе «Ошибки и предложения» (§13.9)."""
    text = _section_text(record, ERRORS_SECTION)
    return {match for match in re.findall(r"/[a-z_]+", text)}


# --- 1. Настоящий sendRichMessage и его структура --------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class RichHelpDeliveryTests(unittest.TestCase):
    """§46–50, §54, §60: пользовательский маршрут /start → «❓ Помощь»."""

    @scenario_test
    async def test_menu_help_button_sends_one_real_rich_message(self, h) -> None:
        await h.text("/start")
        messages_before = [call for call in h.calls if call["type"] == "message"]
        await h.callback("help")

        records = _rich_records(h)
        self.assertEqual(len(records), 1, "справка должна уйти одним Rich Message")

        # По журналу: ровно один вызов именно sendRichMessage (§61), и он не
        # замаскирован под sendMessage.
        rich_calls = [call for call in h.calls if call["type"] == "rich_message"]
        self.assertEqual(len(rich_calls), 1)
        messages_after = [
            call
            for call in h.calls
            if call["type"] == "message" and call not in messages_before
        ]
        self.assertEqual(messages_after, [], "справка ушла обычным sendMessage (§54)")

        # Никакого текста справки в обычных сообщениях чата.
        titles_in_chat = [
            call.get("text") or "" for call in h.calls if call["type"] == "message"
        ]
        self.assertFalse(
            [text for text in titles_in_chat if HELP_TITLE in text],
            "заголовок Rich-справки найден в обычном сообщении",
        )

    @scenario_test
    async def test_rich_message_is_valid_and_uses_blocks_only(self, h) -> None:
        await h.text("/start")
        await h.callback("help")
        record = _rich_records(h)[0]

        payload = record["rich_message"]
        validate_rich_message(payload)  # валидный InputRichMessage
        self.assertEqual(list(payload), ["blocks"], "ровно один способ — blocks")

        self.assertEqual(record["chat_id"], h.chat_id)

        blocks = _blocks_of(record)
        self.assertEqual(blocks[0]["type"], "heading")
        self.assertEqual(blocks[0]["text"], HELP_TITLE)
        self.assertEqual(blocks[0]["size"], 1)
        self.assertIn(HELP_PAGE.hint, _rich_plain_text(record), "подсказка §13")

    @scenario_test
    async def test_nine_collapsed_expandable_sections(self, h) -> None:
        await h.text("/start")
        await h.callback("help")
        record = _rich_records(h)[0]

        details = _details_of(record)
        self.assertEqual(len(details), 9, "§13: в справке девять разделов")
        for block in details:
            self.assertNotIn(
                "is_open",
                block,
                "свёрнутый раздел не передаёт is_open (схема Bot API 10.1)",
            )
            self.assertTrue(block.get("summary"), "пустое название раздела")
            self.assertTrue(block.get("blocks"), "раскрываемый блок без содержимого")

        self.assertEqual([block["summary"] for block in details], list(SECTION_TITLES))

    @scenario_test
    async def test_no_separate_conditional_parts_instruction(self, h) -> None:
        """§17: отдельного раздела об условных частях в справке нет."""
        await h.text("/start")
        await h.callback("help")
        record = _rich_records(h)[0]
        titles = [block["summary"] for block in _details_of(record)]
        self.assertNotIn("Условные части", titles)
        text = _rich_plain_text(record)
        for marker in ("программные выражения", "тире после времени начала"):
            self.assertNotIn(marker, text, f"инструкция §17 попала в справку: {marker}")

    @scenario_test
    async def test_errors_section_matches_registered_commands(self, h) -> None:
        """§13.9: упомянутые команды реально зарегистрированы, ничего не выдумано."""
        await h.text("/start")
        await h.callback("help")
        listed = _commands_in_help(_rich_records(h)[0])
        registered = _registered_commands(h)

        self.assertTrue(listed, "в справке не упомянуто ни одной команды")
        self.assertEqual(listed - registered, set(), "в справке придуманные команды")
        self.assertIn("/report", listed)
        # §13 перечисляет только /report: полного справочника команд больше
        # нет, поэтому полнота регистрации здесь не требуется.

    @scenario_test
    async def test_help_keyboard_has_back_and_about_buttons(self, h) -> None:
        """
        §53 + 2.5.1 + 2.5.4 §7: на Rich Message две кнопки в одной строке —
        «◀️ Назад» (help_back) и «ℹ️ О проекте» (help_about).

        Кнопка «🏠 В меню» с Rich Message убрана: справка возвращает на
        реальный экран-источник, а не безусловно в меню.
        """
        await h.text("/start")
        await h.callback("help")
        record = _rich_records(h)[0]

        buttons = inline_buttons(record["reply_markup"])
        self.assertEqual(buttons, [(BACK_LABEL, BACK_DATA), (ABOUT_LABEL, ABOUT_DATA)])
        self.assertEqual(len(buttons), 2, "на Rich Message две кнопки")
        rows = record["reply_markup"].inline_keyboard
        self.assertEqual(len(rows), 1, "обе кнопки должны стоять в одной строке")
        self.assertNotIn(
            LEGACY_BACK_DATA,
            [data for _label, data in buttons],
            "старая кнопка main_menu осталась на Rich Message",
        )


# --- 2. Возврат, очистка, отсутствие конкурирующих меню --------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class RichHelpReturnTests(unittest.TestCase):
    """§53: возврат восстанавливает меню и убирает временный Rich Message."""

    @scenario_test
    async def test_return_restores_menu_and_deletes_help(self, h) -> None:
        await h.text("/start")
        await h.callback("help")
        rich_id = _rich_records(h)[0]["message_id"]
        self.assertEqual(h.user_data().get(RICH_KEY), rich_id)

        await h.callback(BACK_DATA)

        self.assertIn(rich_id, h.deleted_ids(), "временная справка не удалена")
        self.assertNotIn(RICH_KEY, h.user_data(), "id справки остался в состоянии")
        self.assertIn(cat.text("menu.main"), h.screen_text())
        self.assertEqual(h.screen_buttons(), MENU_BUTTONS)
        # В чате ровно одна живая inline-клавиатура — меню (§6/§53).
        self.assertEqual(len(_live_keyboards(h)), 1, _live_messages(h))
        self.assertEqual(len(_rich_records(h)), 1, "лишний Rich Message")

    @scenario_test
    async def test_help_from_manage_replaces_previous_screen(self, h) -> None:
        await h.text("/start")
        await h.callback("manage")
        manage_id = h.screen_message_id()
        self.assertIn("Управление", h.screen_text())

        await h.callback("help")
        self.assertIn(manage_id, h.deleted_ids(), "прежний экран не закрыт (§53)")
        self.assertEqual(len(_live_keyboards(h)), 1, _live_messages(h))

        await h.callback(BACK_DATA)
        self.assertIn(cat.text("menu.main"), h.screen_text())
        self.assertEqual(h.screen_buttons(), MENU_BUTTONS)
        self.assertEqual(len(_live_keyboards(h)), 1, _live_messages(h))

    @scenario_test
    async def test_help_command_returns_to_menu_too(self, h) -> None:
        await h.text("/start")
        await h.text("/help")
        rich_id = _rich_records(h)[0]["message_id"]

        await h.callback(BACK_DATA)
        self.assertIn(rich_id, h.deleted_ids())
        self.assertIn(cat.text("menu.main"), h.screen_text())

    @scenario_test
    async def test_repeated_help_does_not_accumulate_messages(self, h) -> None:
        await h.text("/start")
        await h.callback("help")
        first_id = _rich_records(h)[0]["message_id"]
        await h.text("/help")

        records = _rich_records(h)
        self.assertEqual(len(records), 2)
        self.assertIn(first_id, h.deleted_ids(), "прежняя справка осталась в чате")
        self.assertNotEqual(h.user_data().get(RICH_KEY), first_id)
        self.assertEqual(len(_live_keyboards(h)), 1, _live_messages(h))

    @scenario_test
    async def test_double_return_does_not_duplicate_menu(self, h) -> None:
        await h.text("/start")
        await h.callback("help")
        await h.callback(BACK_DATA)
        # повторное нажатие уже несуществующей кнопки справки и меню
        await h.callback(BACK_DATA)
        self.assertEqual(h.screen_buttons(), MENU_BUTTONS)
        self.assertEqual(len(_live_keyboards(h)), 1, _live_messages(h))

    @scenario_test
    async def test_restart_does_not_leave_help_keyboard_beside_menu(self, h) -> None:
        """
        §53/§6/§7: перезапуск службы не должен оставлять Rich-справку с живой
        кнопкой возврата рядом с меню (две конкурирующие клавиатуры).

        id временной справки хранится не только в user_data, но и в
        `user_states` (как ref обычного экрана): после рестарта процесса
        первый же экран вытесняет Rich Message — удаляет его. Иначе справка
        с кнопкой «🏠 В меню» осталась бы в чате рядом с новым меню, а id был
        бы потерян навсегда.
        """
        await h.text("/start")
        await h.callback("help")
        rich_id = _rich_records(h)[0]["message_id"]

        await h.restart()  # перезапуск процесса: user_data теряется, БД — нет
        await h.text("/start")

        self.assertNotIn(
            rich_id,
            _live_keyboards(h),
            "кнопка справки осталась рядом с меню: две конкурирующие клавиатуры",
        )
        self.assertIn(rich_id, h.deleted_ids(), "временная справка не удалена")

    @scenario_test
    async def test_help_does_not_touch_delivered_announcement(self, h) -> None:
        """§53/§61: готовый анонс не удаляется вместе со служебным экраном."""
        from tests.test_stage25_delivery_instances import (
            _delivered_without_photos,
            _final_markup,
            _stored,
        )

        await h.text("/start")
        draft = await _delivered_without_photos(h)
        stored = _stored(h, draft.id)
        post_ids = tuple(stored.delivered_message_ids)
        post_text = h.bot.content_text(h.chat_id, post_ids[0])

        await h.callback("help")
        rich_id = _rich_records(h)[0]["message_id"]
        await h.callback(BACK_DATA)

        self.assertIn(rich_id, h.deleted_ids())
        self.assertFalse(
            [message_id for message_id in post_ids if message_id in h.deleted_ids()],
            "удалён готовый анонс пользователя",
        )
        self.assertEqual(h.bot.content_text(h.chat_id, post_ids[0]), post_text)
        markup = _final_markup(h, post_ids[0])
        self.assertTrue(
            [
                data
                for _label, data in inline_buttons(markup)
                if data and data.startswith("anp_")
            ],
            "у анонса пропало управление",
        )

    @scenario_test
    async def test_help_inside_constructor_returns_to_current_step(self, h) -> None:
        """
        2.5.1: справка из конструктора возвращает на ТЕКУЩИЙ шаг черновика.

        Прежний контракт «🏠 В меню» снимал resume-снимок и закрывал
        конструктор. Теперь источник не стирается: «◀️ Назад» ведёт в
        вопрос, на котором автор остановился, а resume-снимок жив (после
        рестарта текст продолжает шаг — H1).
        """
        from handlers.announcement_wizard import RESUME_PAYLOAD_KEY
        from tests.test_stage27b_announcement_ui import (
            _choose_personal,
            _seed_personal_template,
            _seed_place,
            _start_to_template,
        )
        from utils import screen as screen_utils

        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        self.assertRegex(h.screen_text(), cat.question_re(1, 1))

        await h.callback("help")
        self.assertEqual(len(_rich_records(h)), 1)
        self.assertEqual(len(_live_keyboards(h)), 1, _live_messages(h))

        await h.callback(BACK_DATA)
        self.assertRegex(
            h.screen_text(), cat.question_re(1, 1), "не вернулись в конструктор"
        )
        self.assertNotIn(cat.text("menu.main"), h.screen_text())
        payload = screen_utils.load_payload(h.user_id) or {}
        self.assertIn(
            RESUME_PAYLOAD_KEY, payload, "справка сняла resume-снимок конструктора"
        )


# --- 3. Rich Message только в справке --------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class RichHelpIsolationTests(unittest.TestCase):
    """§46/§54: остальные разделы остаются обычными экранами."""

    @scenario_test
    async def test_other_sections_never_send_rich_message(self, h) -> None:
        await h.text("/start")
        visited = []
        for callback in (
            "manage",
            "manage_places",
            "manage_templates",
            "manage_bricks",
            "my_announcements",
        ):
            await h.callback(callback)
            visited.append((callback, h.screen_text().splitlines()[0]))
            self.assertEqual(_rich_records(h), [], f"Rich Message в разделе {callback}")
            self.assertTrue(h.screen_text(), f"раздел {callback} остался без экрана")
            # Экраны разделов по-прежнему выходят в меню прежней кнопкой.
            await h.callback(LEGACY_BACK_DATA)

        await h.callback("pick_place")
        visited.append(("pick_place", h.screen_text().splitlines()[0]))
        self.assertEqual(_rich_records(h), [], "Rich Message в создании анонса")
        self.assertEqual(len(visited), 6)

    @scenario_test
    async def test_announcement_creation_has_no_rich_message(self, h) -> None:
        from tests.test_stage27b_announcement_ui import (
            _choose_personal,
            _seed_personal_template,
            _seed_place,
            _start_to_template,
        )

        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Значение")
        self.assertEqual(_rich_records(h), [], "Rich Message в конструкторе анонса")


# --- 4. Сбой Telegram ------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class RichHelpFailureTests(unittest.TestCase):
    """§54: отказ sendRichMessage — честная реакция, без подмены sendMessage."""

    async def _open_help_with_error(self, h, error) -> str:
        h.bot.errors_by_type["rich_message"] = error
        await h.text("/start")
        await h.callback("help")
        self.assertEqual(
            _rich_records(h), [], "Rich Message не должен считаться отправленным"
        )
        self.assertNotIn(RICH_KEY, h.user_data())
        text = h.screen_text()
        # Никакой подделки: текст справки не показан как обычное сообщение.
        self.assertNotIn(HELP_TITLE, text)
        self.assertNotIn("Условные части", text)
        self.assertIn(cat.text("help.rich.unavailable").split(".")[0], text)
        self.assertEqual(h.screen_buttons(), MENU_BUTTONS)
        return text

    @scenario_test
    async def test_bad_request_shows_clear_message(self, h) -> None:
        await self._open_help_with_error(
            h, BadRequest("Bad Request: unsupported rich message")
        )
        # Возврат из плашки сбоя работает обычным экраном.
        await h.callback(LEGACY_BACK_DATA)
        self.assertIn(cat.text("menu.main"), h.screen_text())

    @scenario_test
    async def test_network_and_flood_errors_are_handled(self, h) -> None:
        """Сетевой сбой и флуд-контроль — та же понятная реакция, не 500."""
        notice = cat.text("help.rich.unavailable").split(".")[0]

        await self._open_help_with_error(h, NetworkError("timed out"))
        await h.text("/start")

        # Telegram просит подождать: повтор после отказа всё равно обработан.
        h.bot.errors_by_type.clear()
        h.bot.errors_by_type["rich_message"] = RetryAfter(3)
        await h.callback("help")
        self.assertEqual(_rich_records(h), [])
        self.assertIn(notice, h.screen_text())

        # Отказ ушёл — справка снова открывается настоящим Rich Message.
        h.bot.errors_by_type.clear()
        await h.text("/start")
        await h.callback("help")
        self.assertEqual(len(_rich_records(h)), 1)

    @scenario_test
    async def test_no_internal_error_notice_is_sent(self, h) -> None:
        """Сбой справки — не 500: пользователь не видит «Что-то пошло не так»."""
        internal = cat.text("errors.internal").split(".")[0]
        h.bot.errors_by_type["rich_message"] = BadRequest("Bad Request: nope")
        await h.text("/start")
        await h.callback("help")

        texts = [
            str(call.get("text") or "") for call in h.calls if call["type"] == "message"
        ]
        self.assertFalse(
            [text for text in texts if internal and internal in text],
            f"показано внутреннее уведомление об ошибке: {texts}",
        )

    @scenario_test
    async def test_journal_distinguishes_send_message_and_rich(self, h) -> None:
        """§61: операции различаются, Rich не растворяется среди sendMessage."""
        await h.text("/start")
        await h.callback("help")
        kinds = [call["type"] for call in h.calls]

        self.assertIn("rich_message", kinds)
        self.assertIn("message", kinds)  # меню — обычное сообщение
        rich = [call for call in h.calls if call["type"] == "rich_message"]
        self.assertEqual(len(rich), 1)
        self.assertEqual(rich[0]["rich_message"]["blocks"][0]["text"], HELP_TITLE)

        # Обычные сообщения не содержат ни заголовка, ни разделов справки.
        plain = [
            str(call.get("text") or "") for call in h.calls if call["type"] == "message"
        ]
        self.assertFalse([text for text in plain if HELP_TITLE in text])
        self.assertFalse([text for text in plain if "Условные части" in text])


class HelpPayloadIsolationTests(unittest.TestCase):
    """Содержимое справки собирается из каталога, а не из сети/нейросети."""

    def test_builder_is_static_and_repeatable(self) -> None:
        """
        Две сборки справки дают одинаковый payload и одинаковые медиа.

        Сравнивать `to_dict()` напрямую нельзя: в `photo.media` лежат живые
        `InputFile` (нужны для hoisting в `send_rich_message`), а у них нет
        `__eq__` — dict-сравнение упирается в identity объекта. Честное
        сравнение — по ДЕТЕРМИНИРОВАННОМУ представлению: после
        `_hoist_input_files` в payload стоят стабильные `attach://rich_media_N`,
        а сами файлы сверяются попарно по имени и содержимому.
        """
        first_files: list[tuple[str, InputFile]] = []
        first = _hoist_input_files(build_help_rich_message().to_dict(), first_files)
        second_files: list[tuple[str, InputFile]] = []
        second = _hoist_input_files(build_help_rich_message().to_dict(), second_files)

        self.assertEqual(first, second, "содержимое справки нестабильно")
        self.assertNotIsInstance(first, str)

        self.assertEqual(
            [name for name, _ in first_files],
            [name for name, _ in second_files],
            "набор медиа справки нестабилен",
        )
        self.assertEqual(
            [(file.filename, file.mimetype) for _, file in first_files],
            [(file.filename, file.mimetype) for _, file in second_files],
        )
        self.assertEqual(
            [file.input_file_content for _, file in first_files],
            [file.input_file_content for _, file in second_files],
            "содержимое картинок справки нестабильно",
        )
        self.assertTrue(first_files, "в справке должны быть файлы слайд-шоу")

    def test_details_are_real_objects_without_shared_state(self) -> None:
        first = build_help_rich_message()
        second = build_help_rich_message()
        self.assertIsNot(first.blocks, second.blocks)


if __name__ == "__main__":
    unittest.main()
