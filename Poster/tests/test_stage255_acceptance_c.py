# tests/test_stage255_acceptance_c.py
"""
Poster 2.5.5, этап C — независимая приёмка пользовательских требований
команды `/report`.

Проверяется ТРЕБОВАНИЕ, а не устройство реализации: сценарии идут через
настоящее приложение, FakeBot и временную SQLite
(`tests/integration_harness.py`); рабочая `post_bot.db`, `.env` и сеть
Telegram не используются. Ожидания формулировок берутся из действующего
каталога `config/ui_texts.yaml` через `tests/ui_texts_expect.py` (yaml —
канон).

Что закреплено.

А. Доступность из любого состояния:
- `/report` из главного меню, посреди диалога анонса (ответ предыдущего
  поля уже сохранён), в режиме ввода значения доставленного анонса, в
  разделе «Площадки» и в групповом чате;
- после отмены возвращается тот же экран/раздел, незавершённые данные и
  клавиатура ввода не теряются.

Б. Сценарий:
- три экрана с точными кнопками каталога и их `callback_data`;
- текст жалобы принимается обычным сообщением (в т.ч. многострочный) и
  показывается подтверждением;
- «Отмена» на каждом этапе возвращает контекст, ничего не сохраняя в БД;
- успешная отправка: подтверждение, затем прежний экран; строка репорта с
  `status=active`, `chat_id`, автором, разделом и хронологическим снимком
  событий ТОЛЬКО этого пользователя за последний час;
- повторный `/report` на каждом этапе не разрушает сохранённый контекст,
  после отправки начинает форму заново;
- повторное «Отправить» и повторная доставка того же update не создают
  второй репорт.

В. Уведомления и просмотр:
- уведомление всем суперадминам с заголовком, автором, разделом и кнопкой
  «Посмотреть» (`rp_view:<id>`);
- `rp_view` суперадмину: полный текст с данными автора отдельным
  сообщением, затем .txt, ПОД сообщениями нет кнопок; повторный просмотр
  не падает и не дублирует репорт;
- `rp_view` обычному пользователю и админу площадки (не суперадмину) —
  отказ, ничего не открыто;
- сбой доставки одному суперадмину: репорт остаётся активным, другому
  доставлено, событие `report.notification_failed` записано;
- .txt: имя файла `report_ГГГГММДД_ЧЧММСС.txt`, содержимое — события за
  час; пустой журнал — короткая отметка каталога, а не пустой файл;
- маркерный текст жалобы НЕ попадает ни в снимок, ни в файл (тело живёт
  только в `reports.body`).

Г. Меню команд:
- `post_init` публикует ровно одну команду `/report` с описанием каталога;
- `/report` не попадает в `unknown_command` и в fallback конструктора.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage255_acceptance_c -v
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from io import BytesIO
from unittest.mock import AsyncMock, patch

from sqlalchemy import text as sqltext
from telegram import (
    BotCommand,
    CallbackQuery,
    Chat,
    Message,
    Update,
    User,
)

import report_store
import utils.user_events as user_events
from models import utcnow

try:
    import bot as bot_module
    from handlers import report as report_module
    from tests import ui_texts_expect as cat
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        FIXTURE_DATE,
        HARNESS_IMPORT_ERROR,
        call_text,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        _choose_personal,
        _open_field_question,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )
    from tests.test_stage255_report_store import _delivered_without_photos

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


SUPERADMIN_ID = 500
SECOND_SUPERADMIN_ID = 501
PLACE_ADMIN_ID = 9_115_001
GROUP_CHAT_ID = 900_002
REPORT_TEXT = "Нажал дату — календарь не открылся"
SECOND_REPORT_TEXT = "Повторная жалоба из другого места"


# --- помощники -------------------------------------------------------------


def _user(user_id: int, username: str | None = None) -> "User":
    """Пользователь Telegram с устойчивым отображаемым именем."""
    return User(
        id=user_id,
        first_name="Тест",
        last_name="Автор",
        username=username,
        is_bot=False,
    )


def _text_update(
    text: str, *, author: "User", chat_id: int, update_id: int, message_id: int
) -> Update:
    """Текстовое сообщение от заданного автора (с username, если он есть)."""
    entities = []
    if text.startswith("/"):
        from telegram import MessageEntity

        command = text.split(maxsplit=1)[0]
        entities = [
            MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(command))
        ]
    message = Message(
        message_id=message_id,
        date=FIXTURE_DATE,
        chat=Chat(id=chat_id, type="private"),
        from_user=author,
        text=text,
        entities=entities,
    )
    return Update(update_id=update_id, message=message)


def _callback_update(
    data: str, *, author: "User", chat_id: int, update_id: int
) -> Update:
    """Нажатие кнопки от заданного автора (с username, если он есть)."""
    message = Message(
        message_id=2,
        date=FIXTURE_DATE,
        chat=Chat(id=chat_id, type="private"),
        from_user=author,
    )
    query = CallbackQuery(
        id=f"cb{update_id}",
        chat_instance="ci",
        from_user=author,
        message=message,
        data=data,
    )
    return Update(update_id=update_id, callback_query=query)


def _form(h, user_id: int = DEFAULT_USER_ID, chat_id: int | None = None) -> dict:
    """Форма репорта чата из `user_states.payload` ({} — формы нет)."""
    report_module.SessionLocal = h.session_factory
    chat = h.chat_id if chat_id is None else chat_id
    return report_module._load_form(user_id, chat) or {}  # noqa: SLF001


def _payload(h, user_id: int = DEFAULT_USER_ID) -> dict:
    report_module.SessionLocal = h.session_factory
    return report_module.screen.load_payload(user_id) or {}


def _reports(h) -> list:
    """Действующие репорты (новые к первым), как их видит разработчик."""
    with h.session_factory() as session:
        return report_store.active_reports(session)[0]


def _documents(h, chat_id: int | None = None) -> list[dict]:
    return [
        call
        for call in h.calls
        if call["type"] == "document"
        and (chat_id is None or call.get("chat_id") == chat_id)
    ]


def _messages(h, chat_id: int) -> list[dict]:
    """Текстовые сообщения конкретному адресату (уведомление, ответ)."""
    return [
        call
        for call in h.calls
        if call["type"] == "message" and call.get("chat_id") == chat_id
    ]


def _document_text(record: dict) -> str:
    document = record.get("document")
    if isinstance(document, BytesIO):
        return document.getvalue().decode("utf-8")
    raise AssertionError(f"вложение не BytesIO: {document!r}")


def _no_unknown_command(case: unittest.TestCase, h) -> None:
    """Команда `/report` не ушла в `unknown_command`/fallback конструктора."""
    unknown = cat.text("screen.unknown_command.neizvestnaya_komanda_naberite_help")
    wizard = cat.text("wizard.handle_unknown_command.konstruktor_anonsa_aktiven")
    shown = list(h.screen_texts()) + list(h.reply_texts())
    for chunk in shown:
        case.assertNotIn(unknown, chunk, f"команда ушла в unknown_command: {chunk!r}")
        case.assertNotIn(wizard, chunk, f"команда ушла в fallback диалога: {chunk!r}")


def _seed_event(h, user_id: int, action: str, *, minutes_ago: int) -> None:
    """Техническое событие журнала с заданным временем (прошлое/чужое)."""
    with h.session_factory() as session:
        session.execute(
            sqltext(
                "INSERT INTO user_event_log "
                "(telegram_user_id, created_at, action, screen, scenario, result, "
                " detail) VALUES (:u, :at, :a, 'seed', NULL, 'ok', NULL)"
            ),
            {
                "u": int(user_id),
                "at": utcnow() - timedelta(minutes=minutes_ago),
                "a": action,
            },
        )
        session.commit()


def _snapshot_lines(snapshot: str) -> list[str]:
    return [line for line in (snapshot or "").splitlines() if " | " in line]


def _values(h, draft_id: int) -> list[tuple]:
    with h.session_factory() as session:
        return session.execute(
            sqltext(
                "SELECT brick_id, state, value_json FROM announcement_values "
                "WHERE draft_id = :draft_id ORDER BY brick_id"
            ),
            {"draft_id": int(draft_id)},
        ).all()


async def _start_wizard(h, *, fields: int = 1):
    """Открывает конструктор и останавливается на первом вопросе."""
    await h.text("/start")
    place_id, _alias = _seed_place(h)
    specs = [
        (f"f{index}", f"Поле {index}", "short_text", {}) for index in range(fields)
    ]
    template_id, _revision, bricks = _seed_personal_template(
        h, name="Шаблон", specs=specs
    )
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    return bricks


def _sent_screen_shown(h) -> bool:
    return any(cat.text("report.screen.sent") in shown for shown in h.screen_texts())


# --- А. Доступность из любого состояния ------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReportAvailabilityTests(unittest.TestCase):
    """Требования 1–5: команда работает из любого состояния."""

    @scenario_test
    async def test_1_from_main_menu(self, h) -> None:
        await h.text("/start")
        menu_text = h.screen_text()
        menu_message_id = h.screen_message_id()

        await h.text("/report")

        cat.assert_shows(self, h.screen_text(), "report.screen.start")
        labels = [label for label, _data in h.screen_buttons()]
        data = [value for _label, value in h.screen_buttons()]
        self.assertEqual(
            labels,
            [cat.text("buttons.report.compose"), cat.text("buttons.report.cancel")],
        )
        self.assertEqual(data, ["rp_compose", "rp_cancel"])
        _no_unknown_command(self, h)
        # Экран меню вытеснен формой, но не потерян в БД.
        self.assertNotEqual(h.screen_text(), menu_text)
        self.assertEqual(h.screen_message_id(), menu_message_id)
        self.assertEqual(_form(h).get("context", {}).get("section"), "main")

        # «Отмена» возвращает ровно то же главное меню.
        await h.callback("rp_cancel")
        self.assertEqual(h.screen_text(), menu_text)
        self.assertEqual(_form(h), {})

    @scenario_test
    async def test_2_inside_wizard_keeps_draft_data(self, h) -> None:
        await h.text("/start")
        _bricks = await _start_wizard(h, fields=2)
        await h.text("ПЕРВОЕ-ЗНАЧЕНИЕ")
        question = h.screen_text()
        state = h.dialog_state()
        draft_values = _values(h, 1)
        resume_payload = _payload(h).get("announcement_resume")
        self.assertIsNotNone(resume_payload, "нет resume-снимка конструктора")

        await h.text("/report")

        cat.assert_shows(self, h.screen_text(), "report.screen.start")
        _no_unknown_command(self, h)
        form = _form(h)
        self.assertEqual(form.get("context", {}).get("section"), "wizard")
        self.assertEqual(form.get("context", {}).get("dialog_state"), state)
        self.assertEqual(h.dialog_state(), state, "состояние диалога изменилось")
        self.assertEqual(_values(h, 1), draft_values, "данные черновика потеряны")
        self.assertEqual(
            _payload(h).get("announcement_resume"),
            resume_payload,
            "resume-снимок конструктора перезаписан",
        )
        self.assertIsNotNone(question)

        # Отмена: тот же вопрос, ответ предыдущего поля цел, ввод идёт в него.
        await h.callback("rp_cancel")
        self.assertEqual(h.dialog_state(), state)
        cat.assert_shows(self, h.screen_text(), "wizard.question.header")
        self.assertEqual(_values(h, 1), draft_values, "отмена потеряла значения")

        await h.text("ВТОРОЕ-ЗНАЧЕНИЕ")
        stored = " ".join(row[2] for row in _values(h, 1))
        self.assertIn("ПЕРВОЕ-ЗНАЧЕНИЕ", stored)
        self.assertIn("ВТОРОЕ-ЗНАЧЕНИЕ", stored)

    @scenario_test
    async def test_3_inside_field_input_of_delivered_announcement(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        await _open_field_question(h)
        input_buttons = h.live_buttons()
        self.assertTrue(
            any(
                value and value.startswith("anq_abort:")
                for _label, value in input_buttons
            ),
            f"не режим ввода поля: {input_buttons}",
        )
        values_before = _values(h, draft.id)
        payload_before = _payload(h).get("announcement_resume")

        await h.text("/report")

        cat.assert_shows(self, h.screen_text(), "report.screen.start")
        self.assertEqual(_form(h).get("context", {}).get("section"), "wizard")

        await h.callback("rp_cancel")
        self.assertEqual(
            h.live_buttons(), input_buttons, "клавиатура ввода поля не восстановлена"
        )
        self.assertEqual(
            _values(h, draft.id), values_before, "значение поля изменилось"
        )

        # Ввод снова работает: текст уходит в поле, а не в подсказку меню.
        await h.text("Значение после репорта")
        stored = " ".join(row[2] for row in _values(h, draft.id))
        self.assertIn("Значение после репорта", stored)
        self.assertEqual(_values(h, draft.id)[0][0], values_before[0][0])
        self.assertIsNotNone(payload_before)

    @scenario_test
    async def test_4_inside_places_section(self, h) -> None:
        await h.text("/start")
        _seed_place(h)
        await h.callback("manage")
        await h.callback("manage_places")
        places_text = h.screen_text()
        places_buttons = h.screen_buttons()
        self.assertTrue(
            any(
                value and value.startswith("plm_open:") for _l, value in places_buttons
            ),
            f"не раздел «Площадки»: {places_buttons}",
        )

        await h.text("/report")

        cat.assert_shows(self, h.screen_text(), "report.screen.start")
        form = _form(h)
        self.assertEqual(form.get("context", {}).get("section"), "places_manage")

        # Отмена с экрана подтверждения — тоже возврат в тот же раздел.
        await h.callback("rp_compose")
        cat.assert_shows(self, h.screen_text(), "report.screen.text")
        await h.text(REPORT_TEXT)
        cat.assert_shows(self, h.screen_text(), "report.screen.confirm")
        await h.callback("rp_cancel")

        self.assertEqual(h.screen_text(), places_text)
        self.assertEqual(h.screen_buttons(), places_buttons)
        self.assertEqual(_reports(h), [], "отмена сохранила репорт")

        # Обычный раздел «Площадки» (выбор места для анонса) — тоже.
        await h.text("/start")
        await h.callback("pick_place")
        pick_text = h.screen_text()
        pick_buttons = h.screen_buttons()
        await h.text("/report")
        self.assertEqual(_form(h).get("context", {}).get("section"), "places")
        await h.callback("rp_cancel")
        self.assertEqual(h.screen_text(), pick_text)
        self.assertEqual(h.screen_buttons(), pick_buttons)

    @scenario_test
    async def test_5_group_chat(self, h) -> None:
        await h.text("/start")
        menu_text = h.screen_text()
        menu_message_id = h.screen_message_id()

        await h.text("/report", chat_id=GROUP_CHAT_ID, chat_type="group")

        form = _form(h, chat_id=GROUP_CHAT_ID)
        self.assertEqual(form.get("stage"), report_module.STAGE_START)
        group_sends = _messages(h, GROUP_CHAT_ID)
        self.assertEqual(len(group_sends), 1)
        self.assertEqual(group_sends[0]["text"], cat.text("report.screen.start"))
        # Экран личного чата не подменён формой группы.
        self.assertFalse(
            [
                value
                for _label, value in h.screen_buttons()
                if value and value.startswith("rp_")
            ],
            "кнопки формы группы попали на экран личного чата",
        )
        self.assertEqual(h.screen_text(), menu_text)
        self.assertEqual(h.screen_message_id(), menu_message_id)
        self.assertEqual(
            [
                (button.text, button.callback_data)
                for row in group_sends[0]["reply_markup"].inline_keyboard
                for button in row
            ],
            [
                (cat.text("buttons.report.compose"), "rp_compose"),
                (cat.text("buttons.report.cancel"), "rp_cancel"),
            ],
        )

        # Шаги в группе правят ТО ЖЕ сообщение, личный экран не трогают.
        await h.callback("rp_compose", chat_id=GROUP_CHAT_ID, chat_type="group")
        self.assertEqual(
            _form(h, chat_id=GROUP_CHAT_ID).get("stage"), report_module.STAGE_TEXT
        )
        await h.callback("rp_cancel", chat_id=GROUP_CHAT_ID, chat_type="group")
        self.assertEqual(_form(h, chat_id=GROUP_CHAT_ID), {})
        self.assertEqual(h.screen_text(), menu_text, "личный экран изменён из группы")


# --- Б. Сценарий -----------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReportScenarioTests(unittest.TestCase):
    """Требования 6–11: экраны формы, сохранение и идемпотентность."""

    @scenario_test
    async def test_6_start_screen_and_cancel(self, h) -> None:
        await h.text("/start")
        menu_text = h.screen_text()

        await h.text("/report")

        cat.assert_shows(self, h.screen_text(), "report.screen.start")
        self.assertEqual(
            h.screen_buttons(),
            [
                (cat.text("buttons.report.compose"), "rp_compose"),
                (cat.text("buttons.report.cancel"), "rp_cancel"),
            ],
        )
        await h.callback("rp_cancel")
        self.assertEqual(h.screen_text(), menu_text)
        self.assertNotIn("report_form", _payload(h))

    @scenario_test
    async def test_7_text_screen_accepts_message(self, h) -> None:
        await h.text("/start")
        await h.text("/report")
        await h.callback("rp_compose")

        cat.assert_shows(self, h.screen_text(), "report.screen.text")
        self.assertEqual(
            h.screen_buttons(),
            [(cat.text("buttons.report.cancel"), "rp_cancel")],
        )
        self.assertEqual(_form(h).get("stage"), report_module.STAGE_TEXT)

        # Обычное сообщение (многострочное) — принимается как описание.
        body = f"{REPORT_TEXT}\nвторая строка"
        await h.text(body)
        cat.assert_shows(self, h.screen_text(), "report.screen.confirm")
        self.assertEqual(_form(h).get("text"), body)

        # Пустой/пробельный текст не принимается, форма ждёт описание.
        await h.callback("rp_cancel")
        await h.text("/report")
        await h.callback("rp_compose")
        await h.text("   ")
        cat.assert_shows(self, h.screen_text(), "report.screen.empty")
        self.assertEqual(_form(h).get("stage"), report_module.STAGE_TEXT)
        self.assertEqual(_reports(h), [])

        # Слишком длинный текст отклоняется с лимитом из каталога.
        limit = report_module.MAX_REPORT_TEXT  # noqa: SLF001 — предел требования
        await h.text("я" * (limit + 1))
        cat.assert_shows(self, h.screen_text(), "report.screen.too_long", limit=limit)
        self.assertEqual(_form(h).get("stage"), report_module.STAGE_TEXT)
        self.assertIsNone(_form(h).get("text"), "длинный текст сохранён в форме")
        self.assertEqual(_reports(h), [])

        # Граничный текст (ровно лимит) принимается.
        await h.text("я" * limit)
        cat.assert_shows(self, h.screen_text(), "report.screen.confirm")
        self.assertEqual(_form(h).get("stage"), report_module.STAGE_CONFIRM)
        self.assertEqual(len(_form(h).get("text") or ""), limit)

    @scenario_test
    async def test_8_confirm_screen_and_cancel(self, h) -> None:
        await h.text("/start")
        menu_text = h.screen_text()
        await h.text("/report")
        await h.callback("rp_compose")
        await h.text(REPORT_TEXT)

        cat.assert_shows(self, h.screen_text(), "report.screen.confirm")
        self.assertEqual(
            h.screen_buttons(),
            [
                (cat.text("buttons.report.send"), "rp_send"),
                (cat.text("buttons.report.cancel"), "rp_cancel"),
            ],
        )
        self.assertEqual(_form(h).get("stage"), report_module.STAGE_CONFIRM)

        await h.callback("rp_cancel")
        self.assertEqual(h.screen_text(), menu_text)
        self.assertEqual(_form(h), {})
        self.assertEqual(_reports(h), [], "отмена на подтверждении создала репорт")

    @scenario_test
    async def test_9_success_stores_report_and_returns_screen(self, h) -> None:
        _bricks = await _start_wizard(h, fields=1)
        question = h.screen_text()
        author = _user(DEFAULT_USER_ID, username="author_x")
        # Событие «чужого» пользователя и события этого пользователя в разное
        # время: снимок обязан взять только свои и только за последний час.
        _seed_event(h, DEFAULT_USER_ID, "seed.early", minutes_ago=45)
        _seed_event(h, DEFAULT_USER_ID, "seed.late", minutes_ago=5)
        _seed_event(h, DEFAULT_USER_ID, "seed.old", minutes_ago=180)
        _seed_event(h, SECOND_SUPERADMIN_ID, "seed.foreign", minutes_ago=5)

        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID,)):
            await h.feed(
                _text_update(
                    "/report",
                    author=author,
                    chat_id=h.chat_id,
                    update_id=900,
                    message_id=50,
                )
            )
            await h.callback("rp_compose")
            await h.feed(
                _text_update(
                    REPORT_TEXT,
                    author=author,
                    chat_id=h.chat_id,
                    update_id=901,
                    message_id=51,
                )
            )
            await h.feed(
                _callback_update(
                    "rp_send", author=author, chat_id=h.chat_id, update_id=902
                )
            )

        self.assertTrue(
            _sent_screen_shown(h), f"нет подтверждения: {h.screen_texts()[-3:]}"
        )
        # Сначала подтверждение, затем прежний экран (тот же вопрос).
        screens = h.screen_texts()
        sent_index = next(
            index
            for index, shown in enumerate(screens)
            if cat.text("report.screen.sent") in shown
        )
        self.assertGreater(sent_index, 0)
        cat.assert_shows(self, screens[sent_index + 1], "wizard.question.header")
        self.assertEqual(h.dialog_state(), 2, "диалог анонса не продолжился")
        self.assertNotEqual(question, "")

        reports = _reports(h)
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertEqual(report.status, report_store.STATUS_ACTIVE)
        self.assertTrue(report.is_active)
        self.assertEqual(report.chat_id, h.chat_id)
        self.assertEqual(report.author_user_id, DEFAULT_USER_ID)
        self.assertEqual(report.username, "author_x")
        self.assertEqual(report.display_name, "Тест Автор")
        self.assertEqual(report.screen_label, cat.text("report.section.wizard"))
        self.assertEqual(report.body, REPORT_TEXT)

        snapshot = report.events_snapshot
        # Только этот пользователь, только последний час, хронологически.
        self.assertIn("seed.early", snapshot)
        self.assertIn("seed.late", snapshot)
        self.assertNotIn("seed.old", snapshot)
        self.assertNotIn("seed.foreign", snapshot)
        self.assertLess(snapshot.index("seed.early"), snapshot.index("seed.late"))
        stamps = [line.split(" | ", 1)[0] for line in _snapshot_lines(snapshot)]
        self.assertEqual(stamps, sorted(stamps), f"снимок не хронологичен: {stamps}")
        # Текст жалобы — только в body, не в снимке.
        self.assertNotIn(REPORT_TEXT, snapshot)

        # Контекст «где был» после отправки доступен для возврата.
        form = _form(h)
        self.assertEqual(form.get("stage"), report_module.STAGE_DONE)
        self.assertEqual(form.get("report_id"), report.id)
        cat.assert_shows(self, question, "wizard.question.header")
        cat.assert_shows(self, screens[sent_index + 1], "wizard.question.header")

    @scenario_test
    async def test_10_repeat_report_keeps_context_and_starts_new_form(self, h) -> None:
        _bricks = await _start_wizard(h, fields=2)
        state = h.dialog_state()
        first_context = None

        async def reach(stage: str) -> None:
            await h.text("/report")
            if stage in ("text", "confirm"):
                await h.callback("rp_compose")
            if stage == "confirm":
                await h.text("Текст, который не отправят")
            self.assertEqual(_form(h).get("stage"), stage)

        expected_stage = {
            "start": report_module.STAGE_START,
            "text": report_module.STAGE_TEXT,
            "confirm": report_module.STAGE_CONFIRM,
        }
        for stage in ("start", "text", "confirm"):
            with self.subTest(repeat_at=stage):
                await reach(stage)
                self.assertEqual(_form(h).get("stage"), expected_stage[stage])
                # Повторный `/report` на любом этапе: только этап меняется.
                await h.text("/report")
                form = _form(h)
                self.assertEqual(form.get("stage"), report_module.STAGE_START)
                self.assertEqual(form.get("context", {}).get("section"), "wizard")
                self.assertEqual(form.get("context", {}).get("dialog_state"), state)
                if first_context is None:
                    first_context = form.get("context")
                else:
                    self.assertEqual(
                        form.get("context"), first_context, "контекст перезаписан"
                    )
                self.assertEqual(h.dialog_state(), state, "диалог анонса сломан")

        # Отмена после трёх входов: тот же диалог, данных не прибавилось.
        await h.callback("rp_cancel")
        self.assertEqual(h.dialog_state(), state)
        cat.assert_shows(self, h.screen_text(), "wizard.question.header")
        self.assertEqual(_values(h, 1), [], "черновик изменился без ответов")

        # Успешная отправка и следующий `/report`: форма начинается заново.
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID,)):
            await h.text("/report")
            await h.callback("rp_compose")
            await h.text(REPORT_TEXT)
            await h.callback("rp_send")
            done_form = _form(h)
            self.assertEqual(done_form.get("stage"), report_module.STAGE_DONE)
            self.assertEqual(done_form.get("text"), REPORT_TEXT)

            await h.text("/report")
            fresh = _form(h)
        self.assertEqual(fresh.get("stage"), report_module.STAGE_START)
        self.assertIsNone(fresh.get("text"), "новая форма унаследовала старый текст")
        self.assertIsNone(fresh.get("report_id"))

        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID,)):
            await h.callback("rp_compose")
            await h.text(SECOND_REPORT_TEXT)
            await h.callback("rp_send")
        bodies = [report.body for report in _reports(h)]
        self.assertEqual(bodies, [SECOND_REPORT_TEXT, REPORT_TEXT])

    @scenario_test
    async def test_11_repeat_send_and_repeated_update_do_not_duplicate(self, h) -> None:
        author = _user(DEFAULT_USER_ID, username="author_x")
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID,)):
            await h.text("/start")
            await h.text("/report")
            await h.callback("rp_compose")
            await h.text(REPORT_TEXT)
            update = _callback_update(
                "rp_send", author=author, chat_id=h.chat_id, update_id=950
            )
            await h.feed(update)
            self.assertEqual(len(_reports(h)), 1)

            # Повторная доставка того же update.
            await h.feed(update)
            self.assertEqual(len(_reports(h)), 1, "повторный update создал репорт")

            # Повторное нажатие той же кнопки (новый update).
            await h.callback("rp_send")
            self.assertEqual(len(_reports(h)), 1, "второе нажатие создало репорт")

        self.assertEqual(
            len(_messages(h, SUPERADMIN_ID)), 1, "уведомление продублировано"
        )
        self.assertEqual(len(_documents(h, SUPERADMIN_ID)), 1)
        self.assertIn(cat.text("report.screen.sent"), h.answer_texts())

    @scenario_test
    async def test_11b_stale_buttons_answer_and_change_nothing(self, h) -> None:
        """Устаревшие кнопки формы: короткий stale-ответ, никаких репортов."""
        await h.text("/start")
        menu_text = h.screen_text()

        before = len(h.calls)
        await h.callback("rp_send")
        calls = h.calls[before:]
        self.assertEqual([call["type"] for call in calls], ["callback_answer"])
        self.assertEqual(calls[0].get("text"), cat.text("report.screen.stale"))
        self.assertTrue(calls[0].get("show_alert"))
        self.assertEqual(_reports(h), [])

        before = len(h.calls)
        await h.callback("rp_compose")
        calls = h.calls[before:]
        self.assertEqual(
            [call["type"] for call in calls],
            ["callback_answer", "edit_message"],
            f"устаревшая кнопка не дала stale-экран: {calls}",
        )
        self.assertEqual(call_text(calls[1]), cat.text("report.screen.stale"))
        self.assertEqual(_reports(h), [])
        self.assertEqual(_form(h), {})

        # «Отмена» без формы — тоже stale, экран даёт путь начать заново.
        await h.callback("rp_cancel")
        cat.assert_shows(self, h.screen_text(), "report.screen.stale")
        # Штатный путь из stale-экрана — сама команда.
        await h.text("/report")
        cat.assert_shows(self, h.screen_text(), "report.screen.start")
        self.assertEqual(_form(h).get("stage"), report_module.STAGE_START)
        await h.callback("rp_cancel")
        self.assertEqual(h.screen_text(), menu_text)


# --- В. Уведомления и просмотр ---------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReportNotificationAndViewTests(unittest.TestCase):
    """Требования 12–17: уведомления, просмотр, файл, приватность."""

    async def _create_report(self, h, body: str = REPORT_TEXT) -> None:
        await h.text("/start")
        await h.text("/report")
        await h.callback("rp_compose")
        await h.text(body)
        await h.callback("rp_send")

    async def _create_report_as(self, h, author: "User", body: str) -> None:
        """Полный цикл формы от имени конкретного автора (с username)."""
        await h.feed(
            _text_update(
                "/report",
                author=author,
                chat_id=h.chat_id,
                update_id=800,
                message_id=60,
            )
        )
        await h.feed(
            _callback_update(
                "rp_compose", author=author, chat_id=h.chat_id, update_id=801
            )
        )
        await h.feed(
            _text_update(
                body,
                author=author,
                chat_id=h.chat_id,
                update_id=802,
                message_id=61,
            )
        )
        await h.feed(
            _callback_update("rp_send", author=author, chat_id=h.chat_id, update_id=803)
        )

    @scenario_test
    async def test_12_notifies_every_superadmin(self, h) -> None:
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID, SECOND_SUPERADMIN_ID)):
            await h.text("/start")
            await self._create_report_as(
                h, _user(DEFAULT_USER_ID, "author_x"), REPORT_TEXT
            )
        report = _reports(h)[0]

        for recipient in (SUPERADMIN_ID, SECOND_SUPERADMIN_ID):
            with self.subTest(recipient=recipient):
                notices = _messages(h, recipient)
                self.assertEqual(len(notices), 1, f"нет уведомления {recipient}")
                shown = notices[0]["text"]
                cat.assert_shows(self, shown, "report.notification.header")
                cat.assert_shows(
                    self,
                    shown,
                    "report.notification.author",
                    author_label="@author_x",
                )
                cat.assert_shows(
                    self,
                    shown,
                    "report.notification.section",
                    screen_label=cat.text("report.section.main"),
                )
                self.assertEqual(
                    [
                        (button.text, button.callback_data)
                        for row in notices[0]["reply_markup"].inline_keyboard
                        for button in row
                    ],
                    [
                        (
                            cat.text("buttons.report.view"),
                            f"rp_view:{report.id}",
                        )
                    ],
                )
                # Вложение .txt — отдельным сообщением тому же адресату.
                self.assertEqual(len(_documents(h, recipient)), 1)
        self.assertEqual(
            report.notified_user_ids, (SUPERADMIN_ID, SECOND_SUPERADMIN_ID)
        )

    @scenario_test
    async def test_13_view_shows_full_text_then_txt_without_buttons(self, h) -> None:
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID,)):
            await self._create_report(h)
        report = _reports(h)[0]
        before = len(h.calls)

        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID,)):
            await h.callback(
                f"rp_view:{report.id}", user_id=SUPERADMIN_ID, chat_id=SUPERADMIN_ID
            )
        view_calls = h.calls[before:]
        self.assertEqual(
            [call["type"] for call in view_calls],
            ["callback_answer", "message", "document"],
        )
        shown = view_calls[1]["text"]
        cat.assert_shows(self, shown, "report.view.title", report_id=report.id)
        cat.assert_shows(self, shown, "report.view.author", author_label="Тест")
        cat.assert_shows(self, shown, "report.view.section")
        cat.assert_shows(self, shown, "report.view.body", body=REPORT_TEXT)
        self.assertIsNone(view_calls[1].get("reply_markup"), "кнопки под текстом")
        self.assertIsNone(view_calls[2].get("reply_markup"), "кнопки под файлом")
        self.assertEqual(_document_text(view_calls[2]), report.events_snapshot)

        # Повторный просмотр: не падает и не дублирует репорт.
        before = len(h.calls)
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID,)):
            await h.callback(
                f"rp_view:{report.id}", user_id=SUPERADMIN_ID, chat_id=SUPERADMIN_ID
            )
        repeat = h.calls[before:]
        self.assertEqual(
            [call["type"] for call in repeat],
            ["callback_answer", "message", "document"],
        )
        self.assertEqual(len(_reports(h)), 1, "просмотр создал второй репорт")
        self.assertEqual(repeat[1]["text"], shown)

    @scenario_test
    async def test_14_view_denied_to_regular_user_and_place_admin(self, h) -> None:
        from models import PlaceAdmin
        from place_store import create_place
        from user_store import get_or_create_user

        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID,)):
            await self._create_report(h)
        report = _reports(h)[0]

        # Админ площадки (не суперадмин): роль реальная, в списке его нет.
        with h.session_factory() as session:
            admin = get_or_create_user(session, PLACE_ADMIN_ID, username="place_admin")
            place = create_place(
                session,
                "Дом культуры",
                city="Тестовый город",
                created_by_user_id=admin.id,
                address=None,
                url=None,
            )
            session.add(
                PlaceAdmin(
                    place_id=place.id,
                    user_id=admin.id,
                    assigned_by_user_id=None,
                    assigned_at=utcnow(),
                )
            )
            session.commit()

        denial = cat.text("admin.render_phase.deystvie_dostupno_tolko")
        for user_id, chat_id in (
            (PLACE_ADMIN_ID, h.chat_id + 1),
            (SECOND_SUPERADMIN_ID, h.chat_id + 2),
        ):
            with self.subTest(user_id=user_id):
                before = len(h.calls)
                await h.callback(
                    f"rp_view:{report.id}",
                    user_id=user_id,
                    chat_id=chat_id,
                )
                calls = h.calls[before:]
                self.assertEqual(
                    [call["type"] for call in calls],
                    ["callback_answer"],
                    f"не-суперадмин получил данные репорта: {calls}",
                )
                self.assertEqual(calls[0].get("text"), denial)
                self.assertTrue(calls[0].get("show_alert"))

        # Владелец репорта (обычный автор) — тоже отказ.
        before = len(h.calls)
        await h.callback(f"rp_view:{report.id}")
        self.assertEqual(
            [call["type"] for call in h.calls[before:]], ["callback_answer"]
        )

        # Несуществующий репорт не раскрывает ничего даже суперадмину.
        before = len(h.calls)
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID,)):
            await h.callback(
                "rp_view:999", user_id=SUPERADMIN_ID, chat_id=SUPERADMIN_ID
            )
        self.assertEqual(
            [call["type"] for call in h.calls[before:]], ["callback_answer"]
        )
        self.assertEqual(h.calls[-1].get("text"), cat.text("report.view.missing"))

    @scenario_test
    async def test_15_undelivered_notice_keeps_report_active(self, h) -> None:
        original = h.bot.send_message

        async def flaky(*args, **kwargs):
            chat_id = kwargs.get("chat_id")
            if chat_id is None and args:
                chat_id = args[0]
            if chat_id == SECOND_SUPERADMIN_ID:
                raise RuntimeError("имитация сбоя доставки")
            return await original(*args, **kwargs)

        h.bot.send_message = flaky
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID, SECOND_SUPERADMIN_ID)):
            await self._create_report(h)
        h.bot.send_message = original

        reports = _reports(h)
        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0].is_active, "сбой уведомления снял репорт")
        self.assertEqual(reports[0].notified_user_ids, (SUPERADMIN_ID,))
        self.assertEqual(len(_messages(h, SUPERADMIN_ID)), 1, "второму не доставлено")
        self.assertEqual(len(_documents(h, SUPERADMIN_ID)), 1)

        failed = [
            event
            for event in user_events.list_user_events(
                SECOND_SUPERADMIN_ID, session_factory=h.session_factory
            )
            if event.action == "report.notification_failed"
        ]
        self.assertEqual(len(failed), 1, "сбой доставки не записан в журнал")
        self.assertEqual(failed[0].result, "error")
        self.assertIn(f"report_id={reports[0].id}", failed[0].detail or "")
        self.assertIn("kind=message", failed[0].detail or "")
        self.assertTrue(_sent_screen_shown(h), "автор не увидел результат")

    @scenario_test
    async def test_16_txt_file_named_and_not_empty(self, h) -> None:
        _seed_event(h, DEFAULT_USER_ID, "seed.early", minutes_ago=40)
        _seed_event(h, DEFAULT_USER_ID, "seed.old", minutes_ago=240)
        _seed_event(h, SECOND_SUPERADMIN_ID, "seed.foreign", minutes_ago=1)
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID,)):
            await self._create_report(h)

        documents = _documents(h, SUPERADMIN_ID)
        self.assertEqual(len(documents), 1)
        record = documents[0]
        self.assertRegex(record["filename"], r"^report_\d{8}_\d{6}\.txt$")
        content = _document_text(record)
        self.assertIn(f"События пользователя {DEFAULT_USER_ID}", content)
        self.assertIn("seed.early", content)
        self.assertNotIn("seed.old", content)
        self.assertNotIn("seed.foreign", content)
        self.assertEqual(content, _reports(h)[0].events_snapshot)

        # Журнал пуст к моменту отправки: короткая отметка, а не пустой файл.
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID,)):
            await h.text("/report")
            await h.callback("rp_compose")
            await h.text(SECOND_REPORT_TEXT)
            with h.session_factory() as session:
                session.execute(sqltext("DELETE FROM user_event_log"))
                session.commit()
            await h.callback("rp_send")
        empty_documents = _documents(h, SUPERADMIN_ID)
        self.assertEqual(len(empty_documents), 2)
        empty_content = _document_text(empty_documents[-1])
        self.assertIn(cat.text("report.events.empty"), empty_content)
        self.assertEqual(empty_content, _reports(h)[0].events_snapshot)

    @scenario_test
    async def test_17_no_user_content_in_snapshot_or_file(self, h) -> None:
        marker = "МАРКЕР-ЖАЛОБЫ-77"
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID,)):
            await h.text("/start")
            await h.text("/report")
            await h.callback("rp_compose")
            await h.text(f"{marker}\nвторая строка жалобы")
            await h.callback("rp_send")

        report = _reports(h)[0]
        self.assertIn(marker, report.body, "текст жалобы не сохранён в body")
        self.assertNotIn(marker, report.events_snapshot, "маркер попал в снимок")
        self.assertNotIn("вторая строка жалобы", report.events_snapshot)
        content = _document_text(_documents(h, SUPERADMIN_ID)[0])
        self.assertNotIn(marker, content, "маркер попал в .txt")
        events = user_events.list_user_events(
            DEFAULT_USER_ID, session_factory=h.session_factory
        )
        self.assertNotIn(
            marker,
            " ".join(f"{event.detail or ''} {event.result or ''}" for event in events),
            "маркер попал в журнал событий",
        )
        # При этом сам разработчик текст видит — в просмотре репорта.
        before = len(h.calls)
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID,)):
            await h.callback(
                f"rp_view:{report.id}", user_id=SUPERADMIN_ID, chat_id=SUPERADMIN_ID
            )
        self.assertIn(marker, h.calls[before + 1]["text"])
        self.assertIn(marker, report.body)


# --- Г. Меню команд --------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReportCommandsTests(unittest.TestCase):
    """Требования 18–19: публичный список команд и отсутствие fallback."""

    @scenario_test
    async def test_18_post_init_publishes_only_report(self, h) -> None:
        self.assertIs(h.app.post_init, bot_module.post_init_application)
        with patch.object(
            bot_module, "post_init_announcement_cleanup", new=AsyncMock()
        ) as cleanup:
            await bot_module.post_init_application(h.app)
        self.assertEqual(cleanup.await_count, 1, "post_init не выполнил вторую задачу")

        published = [call for call in h.calls if call["type"] == "set_my_commands"]
        self.assertEqual(len(published), 1, "список команд не опубликован")
        commands = published[0]["args"][0]
        self.assertEqual(
            [(command.command, command.description) for command in commands],
            [("report", cat.text("report.command.description"))],
        )
        self.assertIsInstance(commands[0], BotCommand)

    @scenario_test
    async def test_19_report_never_falls_back_to_unknown_command(self, h) -> None:
        await h.text("/start")
        await h.text("/report")
        cat.assert_shows(self, h.screen_text(), "report.screen.start")
        _no_unknown_command(self, h)
        await h.callback("rp_cancel")

        # Внутри диалога анонса команда тоже не уходит в fallback конструктора.
        await _start_wizard(h, fields=1)
        question = h.screen_text()
        await h.text("/report")
        cat.assert_shows(self, h.screen_text(), "report.screen.start")
        _no_unknown_command(self, h)
        self.assertNotEqual(h.screen_text(), question)
        self.assertEqual(h.dialog_state(), 2, "команда сломала диалог анонса")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
