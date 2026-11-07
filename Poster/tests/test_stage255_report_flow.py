# tests/test_stage255_report_flow.py
"""
Этап C: команда `/report` и минимальный просмотр репорта.

Проверяется сквозной путь через настоящий `Application.process_update`:

1. `/report` из главного меню — экран-вопрос и кнопки;
2. `/report` внутри диалога анонса посреди ввода вопроса: команду
   обрабатывает наш handler, а не fallback `.*`/`unknown_command`,
   состояние диалога не меняется;
3. `/report` на шаге ввода даты: после отмены экран шага возвращается и
   ввод продолжает попадать в то же поле;
4. отмена на каждом этапе (start/text/confirm) с восстановлением;
5. полный цикл: строка в `reports`, уведомление суперадминистраторам,
   .txt-снимок событий, `record_notification`, события `report.*`;
6. автор без username — подпись берётся из имени (и ID как последний
   фолбэк);
7. повторный `/report` во время формы не теряет исходный контекст, а
   повторное «Отправить» не создаёт второй репорт;
8. `/report` в группе: своё сообщение формы, экраны/клавиатуры группы и
   личный экран не тронуты, формы хранятся по чату, отмена закрывает
   только сообщение этого чата;
9. просмотр `rp_view`: проверка прав на каждом вызове, полный текст с
   данными автора + .txt без кнопок, отметка пустого снимка,
   `set_my_commands` из `post_init` и недоставленное уведомление.

Тексты сверяются по каталогу (`tests/ui_texts_expect`), а не литералами:
формулировки правит владелец `config/ui_texts.yaml`. Работают только
временная SQLite-база и FakeBot: сеть и рабочая `post_bot.db` не нужны.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from io import BytesIO
from unittest.mock import patch

from sqlalchemy import text as sqltext
from telegram import BotCommand

import report_store
import utils.user_events as user_events

try:
    import bot as bot_module
    from handlers import report as report_module
    from tests import integration_harness as harness
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        _callback,
        _choose_personal,
        _open_field_question,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )
    from tests.test_stage255_report_store import _delivered_without_photos
    from tests.ui_texts_expect import assert_shows, text

    IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    IMPORT_ERROR = str(exc)


SUPERADMIN_ID = 500
OTHER_SUPERADMIN_ID = 501
REPORT_TEXT = "Нажал дату — календарь не открылся"
GROUP_CHAT_ID = 900_002


def _form(h, user_id: int = DEFAULT_USER_ID, chat_id: int | None = None) -> dict:
    """Форма репорта чата из `user_states.payload` ({} — формы нет)."""
    report_module.SessionLocal = h.session_factory
    chat = h.chat_id if chat_id is None else chat_id
    form = report_module._load_form(user_id, chat)  # noqa: SLF001 — проверка снимка
    return form or {}


def _payload(h, user_id: int = DEFAULT_USER_ID) -> dict:
    report_module.SessionLocal = h.session_factory
    return report_module.screen.load_payload(user_id) or {}


def _reports(h) -> list:
    with h.session_factory() as session:
        return report_store.active_reports(session)[0]


def _documents(h, chat_id: int | None = None) -> list[dict]:
    return [
        call
        for call in h.calls
        if call["type"] == "document"
        and (chat_id is None or call.get("chat_id") == chat_id)
    ]


def _notifications(h, chat_id: int) -> list[dict]:
    """Текстовые уведомления конкретному адресату (не экран и не пост)."""
    return [
        call
        for call in h.calls
        if call["type"] == "message"
        and call.get("chat_id") == chat_id
        and call.get("link_preview_options") is None
    ]


def _document_text(record: dict) -> str:
    document = record.get("document")
    if isinstance(document, BytesIO):
        return document.getvalue().decode("utf-8")
    raise AssertionError(f"вложение не BytesIO: {document!r}")


def _no_unknown_command(h) -> None:
    """Fallback `unknown_command`/диалога на команду НЕ срабатывал."""
    catalog_unknown = text("screen.unknown_command.neizvestnaya_komanda_naberite_help")
    catalog_wizard = text("wizard.handle_unknown_command.konstruktor_anonsa_aktiven")
    for shown in h.screen_texts():
        assert (
            catalog_unknown not in shown
        ), f"команда ушла в unknown_command: {shown!r}"
        assert (
            catalog_wizard not in shown
        ), f"команда ушла в fallback диалога: {shown!r}"


def _assert_report_start(self, h) -> None:
    assert_shows(self, h.screen_text(), "report.screen.start")
    labels = [label for label, _data in h.screen_buttons()]
    self.assertIn(text("buttons.report.compose"), labels)
    self.assertIn(text("buttons.report.cancel"), labels)


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class ReportFlowTests(unittest.TestCase):
    # —— блоки 1–3: команда из любого состояния ————————————————————————

    @scenario_test
    async def test_block1_report_from_main_menu(self, h) -> None:
        """`/report` из меню: экран вопроса, кнопки и сохранение контекста."""
        await h.text("/start")
        await h.text("/report")
        _assert_report_start(self, h)
        _no_unknown_command(h)

        form = _form(h)
        self.assertEqual(form.get("stage"), report_module.STAGE_START)
        self.assertEqual(form.get("context", {}).get("section"), "main")
        self.assertIsNone(form.get("context", {}).get("dialog_state"))

        events = user_events.list_user_events(
            DEFAULT_USER_ID, session_factory=h.session_factory
        )
        self.assertEqual([event.action for event in events], ["report.started"])
        self.assertEqual(events[0].screen, "main")

    @scenario_test
    async def test_block2_report_inside_wizard_text_question(self, h) -> None:
        """Команда посреди вопроса анонса не ломает диалог и его ввод."""
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Шаблон",
            specs=[("main", "Основное поле", "short_text", {})],
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        question_screen = h.screen_text()
        state = h.dialog_state()

        await h.text("/report")
        _assert_report_start(self, h)
        _no_unknown_command(h)
        self.assertEqual(h.dialog_state(), state, "состояние диалога изменилось")

        form = _form(h)
        self.assertEqual(form.get("context", {}).get("section"), "wizard")
        self.assertEqual(form.get("context", {}).get("dialog_state"), state)
        # Resume-снимок конструктора цел: `/report` пишет свой ключ.
        self.assertIn("announcement_resume", _payload(h))

        # Отмена возвращает тот же вопрос, и ввод снова идёт в конструктор.
        await h.callback("rp_cancel")
        self.assertEqual(h.screen_text(), question_screen)
        await h.text("Значение после репорта")
        self.assertNotIn(question_screen, h.screen_text())

    @scenario_test
    async def test_block3_report_inside_date_input(self, h) -> None:
        """`/report` на вопросе даты: отмена возвращает шаг и ввод работает."""
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Дата",
            specs=[("when", "Дата события", "date", {})],
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        date_screen = h.screen_text()
        self.assertTrue(_callback(h, "aw_calday"), "не шаг даты")

        await h.text("/report")
        _assert_report_start(self, h)
        _no_unknown_command(h)

        await h.callback("rp_cancel")
        self.assertEqual(h.screen_text(), date_screen)
        self.assertEqual(h.dialog_state(), 2)  # AW_QUESTIONS
        # Ввод даты продолжает тот же шаг: экран уходит с вопроса.
        await h.text("20.11.2026")
        self.assertNotEqual(h.screen_text(), date_screen)

    @scenario_test
    async def test_block3b_report_inside_field_input_of_delivered(self, h) -> None:
        """
        `/report` в режиме ввода поля доставленного анонса (2.5.4 §1).

        Ввод живёт на самом анонсе и не является шагом диалога
        (`_restore_wizard` показал бы список полей). Возврат обязан вернуть
        клавиатуру ввода поля, иначе следующий текст пропал бы в подсказке
        меню, а значение не записалось.
        """
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        await _open_field_question(h)
        input_buttons = h.live_buttons()
        self.assertTrue(
            any(
                data and data.startswith("anq_abort:") for _label, data in input_buttons
            ),
            f"не ввод поля: {input_buttons}",
        )

        await h.text("/report")
        _assert_report_start(self, h)
        _no_unknown_command(h)
        self.assertEqual(_form(h)["context"]["section"], "wizard")

        await h.callback("rp_cancel")
        self.assertEqual(h.live_buttons(), input_buttons, "ввод поля не восстановлен")
        self.assertIn("announcement_resume", _payload(h))

        await h.text("Значение после репорта")
        with h.session_factory() as session:
            stored = session.execute(
                sqltext(
                    "SELECT value_json FROM announcement_values "
                    "WHERE draft_id = :draft_id"
                ),
                {"draft_id": draft.id},
            ).all()
        self.assertIn("Значение после репорта", " ".join(row[0] for row in stored))

    # —— блок 4: отмена на каждом этапе ————————————————————————————————

    @scenario_test
    async def test_block4_cancel_on_every_stage(self, h) -> None:
        """«Отмена» на start/text/confirm возвращает прежний экран меню."""
        await h.text("/start")
        menu_text = h.screen_text()

        await h.text("/report")
        await h.callback("rp_cancel")
        self.assertEqual(h.screen_text(), menu_text)
        self.assertEqual(_form(h), {}, "форма не снята")
        self.assertNotIn("report_form", _payload(h))

        # Этап text: отмена на экране описания.
        await h.text("/report")
        await h.callback("rp_compose")
        assert_shows(self, h.screen_text(), "report.screen.text")
        await h.callback("rp_cancel")
        self.assertEqual(h.screen_text(), menu_text)

        # Этап confirm: отмена после сохранения текста.
        await h.text("/report")
        await h.callback("rp_compose")
        await h.text("Описание")
        assert_shows(self, h.screen_text(), "report.screen.confirm")
        await h.callback("rp_cancel")
        self.assertEqual(h.screen_text(), menu_text)

        with h.session_factory() as session:
            self.assertEqual(report_store.active_reports(session)[1], 0)
        events = user_events.list_user_events(
            DEFAULT_USER_ID, session_factory=h.session_factory
        )
        self.assertEqual(
            [event.action for event in events],
            [
                "report.started",
                "report.cancelled",
                "report.started",
                "report.cancelled",
                "report.started",
                "report.text_received",
                "report.cancelled",
            ],
        )

    # —— блок 5: полный цикл ——————————————————————————————————————————

    @scenario_test
    async def test_block5_full_cycle_notifies_with_txt(self, h) -> None:
        """Отправка: строка репорта, уведомление, .txt, отметка доставки."""
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID,)):
            await h.text("/start")
            await h.text("/report")
            await h.callback("rp_compose")
            await h.text(REPORT_TEXT)
            await h.callback("rp_send")

        screens = h.screen_texts()
        self.assertTrue(
            any(text("report.screen.sent") in shown for shown in screens),
            f"нет подтверждения отправки: {screens[-3:]!r}",
        )
        _no_unknown_command(h)

        reports = _reports(h)
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertEqual(report.author_user_id, DEFAULT_USER_ID)
        self.assertEqual(report.body, REPORT_TEXT)
        self.assertEqual(report.screen_label, text("report.section.main"))
        self.assertEqual(report.notified_user_ids, (SUPERADMIN_ID,))
        self.assertTrue(report.is_active)

        # Снимок в БД: заголовок пользователя и событие этой же формы.
        self.assertIn(f"События пользователя {DEFAULT_USER_ID}", report.events_snapshot)
        self.assertIn("report.text_received", report.events_snapshot)

        notifications = _notifications(h, SUPERADMIN_ID)
        self.assertEqual(len(notifications), 1)
        shown = notifications[0]["text"]
        self.assertIn(text("report.notification.header"), shown)
        self.assertIn("От: Тест", shown)
        self.assertIn(text("report.section.main"), shown)
        # Снимок собирается ДО вставки строки: в нём навигация и приём
        # текста, а само `report.sent` пишется уже после создания репорта.
        self.assertIn(
            "report.text_received", _document_text(_documents(h, SUPERADMIN_ID)[0])
        )
        views = harness.inline_buttons(notifications[0].get("reply_markup"))
        self.assertEqual(views, [(text("buttons.report.view"), f"rp_view:{report.id}")])

        documents = _documents(h, SUPERADMIN_ID)
        self.assertEqual(len(documents), 1)
        self.assertRegex(documents[0]["filename"], r"^report_\d{8}_\d{6}\.txt$")
        self.assertIn(
            f"События пользователя {DEFAULT_USER_ID}",
            _document_text(documents[0]),
        )

        events = user_events.list_user_events(
            DEFAULT_USER_ID, session_factory=h.session_factory
        )
        self.assertEqual(
            [event.action for event in events],
            ["report.started", "report.text_received", "report.sent"],
        )
        # Журнал технический: самого текста жалобы в событиях нет.
        self.assertNotIn(
            REPORT_TEXT,
            " ".join(f"{event.result or ''} {event.detail or ''}" for event in events),
        )

    @scenario_test
    async def test_block6_author_without_username(self, h) -> None:
        """Нет username — в уведомлении имя автора; ID — последний фолбэк."""
        self.assertIn(
            text("report.notification.author", author_label="Тест"),
            report_module._notification_text(  # noqa: SLF001 — сборка строки
                report_store.Report(
                    id=1,
                    author_user_id=42,
                    chat_id=42,
                    username=None,
                    display_name="Тест",
                    body="x",
                    screen_label="main",
                    created_at=datetime(2026, 1, 1, 12, 0),
                    events_snapshot="",
                    status=report_store.STATUS_ACTIVE,
                    developer_reply=None,
                    replied_at=None,
                    closed_at=None,
                    notified_user_ids=(),
                )
            ),
        )
        self.assertEqual(
            report_module._author_label(None, None, 42),  # noqa: SLF001
            "42",
        )
        self.assertEqual(
            report_module._author_label("author", "Имя", 42),  # noqa: SLF001
            "@author",
        )

        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID,)):
            await h.text("/start")
            await h.text("/report")
            await h.callback("rp_compose")
            await h.text(REPORT_TEXT)
            await h.callback("rp_send")
        shown = _notifications(h, SUPERADMIN_ID)[0]["text"]
        self.assertIn(text("report.notification.author", author_label="Тест"), shown)

    # —— блок 7: повторный вход и идемпотентность ——————————————————————

    @scenario_test
    async def test_block7_repeat_report_keeps_context_and_send_is_idempotent(
        self, h
    ) -> None:
        """Повторный `/report` меняет только этап; второе «Отправить» — без дубля."""
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID,)):
            await h.text("/start")
            place_id, _alias = _seed_place(h)
            template_id, _revision, _bricks = _seed_personal_template(
                h,
                name="Шаблон",
                specs=[("main", "Основное поле", "short_text", {})],
            )
            await _start_to_template(h, place_id)
            await _choose_personal(h, template_id)
            state = h.dialog_state()

            await h.text("/report")
            first_context = _form(h)["context"]
            await h.callback("rp_compose")
            await h.text(REPORT_TEXT)

            # Повторный `/report` посреди формы: этап сброшен, контекст тот же.
            await h.text("/report")
            form = _form(h)
            self.assertEqual(form.get("stage"), report_module.STAGE_START)
            self.assertEqual(form.get("context"), first_context)
            self.assertEqual(first_context.get("section"), "wizard")

            await h.callback("rp_compose")
            await h.text(REPORT_TEXT)
            await h.callback("rp_send")
            self.assertEqual(len(_reports(h)), 1)
            self.assertEqual(len(_notifications(h, SUPERADMIN_ID)), 1)

            # Двойное нажатие «Отправить»: второй репорт не создаётся.
            await h.callback("rp_send")
            self.assertEqual(len(_reports(h)), 1)
            self.assertEqual(len(_notifications(h, SUPERADMIN_ID)), 1)
            self.assertEqual(h.dialog_state(), state)

    # —— блок 8: просмотр, пустой снимок, права, post_init ————————————

    @scenario_test
    async def test_block10_report_in_group_chat(self, h) -> None:
        """
        `/report` в группе: отдельное сообщение формы, экран/клавиатуры
        группы не тронуты, форма хранится по чату, отмена закрывает её.

        Экран «бот как приложение» принадлежит личному чату: правка его
        сообщения показала бы форму посторонним, поэтому в группе форма
        живёт собственным сообщением бота.
        """
        group_id = GROUP_CHAT_ID
        await h.text("/start")
        menu_text = h.screen_text()
        menu_message_id = h.screen_message_id()
        private_calls_before = len(h.calls)

        # Команда в группе: свой экран личного чата не перерисовывается.
        await h.text("/report", chat_id=group_id, chat_type="group")
        form = _form(h, chat_id=group_id)
        self.assertEqual(form.get("stage"), report_module.STAGE_START)
        self.assertEqual(form.get("context"), {"section": "main", "dialog_state": None})
        self.assertIsInstance(form.get("message_id"), int)
        # Форма по chat-ключу: в личном чате её нет (и наоборот).
        self.assertEqual(_form(h), {})
        self.assertEqual(h.screen_text(), menu_text)
        self.assertEqual(h.screen_message_id(), menu_message_id)

        form_message_id = form["message_id"]
        group_sends = [
            call
            for call in h.calls
            if call["type"] == "message" and call.get("chat_id") == group_id
        ]
        self.assertEqual(len(group_sends), 1)
        self.assertEqual(harness.call_text(group_sends[0]), text("report.screen.start"))
        self.assertEqual(
            harness.inline_buttons(group_sends[0].get("reply_markup")),
            [
                (text("buttons.report.compose"), "rp_compose"),
                (text("buttons.report.cancel"), "rp_cancel"),
            ],
        )

        # «Составить репорт»: правится ТО ЖЕ сообщение формы, новых нет.
        await h.callback("rp_compose", chat_id=group_id, chat_type="group")
        form = _form(h, chat_id=group_id)
        self.assertEqual(form.get("stage"), report_module.STAGE_TEXT)
        self.assertEqual(form.get("message_id"), form_message_id)
        group_edits = [
            call
            for call in h.calls
            if call["type"] == "edit_message" and call.get("chat_id") == group_id
        ]
        self.assertEqual(len(group_edits), 1)
        self.assertEqual(group_edits[0]["message_id"], form_message_id)
        self.assertIn(text("report.screen.text"), group_edits[0]["text"])

        # Личный экран за всё время в группе не тронут: ни правок, ни
        # новых сообщений в личном чате (форма живёт в своём сообщении).
        self.assertEqual(h.screen_text(), menu_text)
        self.assertEqual(h.screen_message_id(), menu_message_id)
        self.assertEqual(
            [
                call
                for call in h.calls[private_calls_before:]
                if call.get("chat_id") == h.chat_id
                and call["type"] in ("message", "edit_message")
            ],
            [],
            "в личный чат ушли сообщения при работе формы в группе",
        )

        # Формы по чату независимы: личная форма не подменяет групповую.
        await h.text("/report")
        self.assertEqual(_form(h).get("stage"), report_module.STAGE_START)
        self.assertEqual(_form(h, chat_id=group_id).get("stage"), "text")

        # Отмена в группе: удаляется только сообщение формы этого чата,
        # личная форма и её экран остаются.
        await h.callback("rp_cancel", chat_id=group_id, chat_type="group")
        self.assertEqual(_form(h, chat_id=group_id), {})
        self.assertEqual(_form(h).get("stage"), report_module.STAGE_START)
        self.assertIn(form_message_id, h.deleted_ids())
        assert_shows(self, h.screen_text(), "report.screen.start")

        await h.callback("rp_cancel")
        self.assertEqual(_form(h), {})
        self.assertNotIn("report_form", _payload(h))

        events = user_events.list_user_events(
            DEFAULT_USER_ID, session_factory=h.session_factory
        )
        # Групповая и личная формы пишут в ОДИН журнал пользователя.
        self.assertEqual(
            [event.action for event in events],
            [
                "report.started",
                "report.started",
                "report.cancelled",
                "report.cancelled",
            ],
        )

        # Полный цикл ИЗ ГРУППЫ: репорт сохраняется с chat_id группы,
        # сообщение формы становится подтверждением без кнопок, а личный
        # экран по-прежнему не трогается (восстанавливать там нечего).
        menu_text = h.screen_text()
        private_calls_before = len(h.calls)
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID,)):
            await h.text("/report", chat_id=group_id, chat_type="group")
            await h.callback("rp_compose", chat_id=group_id, chat_type="group")
            await h.text(REPORT_TEXT, chat_id=group_id, chat_type="group")
            await h.callback("rp_send", chat_id=group_id, chat_type="group")

        reports = _reports(h)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].chat_id, group_id)
        self.assertEqual(reports[0].body, REPORT_TEXT)
        self.assertEqual(reports[0].screen_label, text("report.section.main"))
        self.assertEqual(_form(h, chat_id=group_id), {})
        group_screens = [
            call
            for call in h.calls
            if call.get("chat_id") == group_id
            and call["type"] in ("message", "edit_message")
        ]
        self.assertEqual(
            harness.call_text(group_screens[-1]), text("report.screen.sent")
        )
        self.assertIsNone(group_screens[-1].get("reply_markup"))
        self.assertEqual(h.screen_text(), menu_text)
        self.assertEqual(
            [
                call
                for call in h.calls[private_calls_before:]
                if call.get("chat_id") == h.chat_id
                and call["type"] in ("message", "edit_message")
            ],
            [],
        )

    @scenario_test
    async def test_block8_view_permissions_empty_snapshot_and_commands(self, h) -> None:
        """`rp_view` только суперадмину; пустой снимок и `set_my_commands`."""
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID,)):
            await h.text("/start")
            await h.text("/report")
            await h.callback("rp_compose")
            await h.text(REPORT_TEXT)
            await h.callback("rp_send")
        report = _reports(h)[0]

        # Чужой (не суперадминистратор) не получает ни текста, ни файла.
        before = len(h.calls)
        await h.callback(f"rp_view:{report.id}")
        view_calls = h.calls[before:]
        self.assertEqual([call["type"] for call in view_calls], ["callback_answer"])
        self.assertTrue(view_calls[0].get("show_alert"))

        # Суперадминистратор: полный текст отдельным сообщением и .txt.
        before = len(h.calls)
        with patch("config.SUPERADMIN_USER_IDS", (DEFAULT_USER_ID,)):
            await h.callback(f"rp_view:{report.id}")
        view_calls = h.calls[before:]
        kinds = [call["type"] for call in view_calls]
        self.assertEqual(kinds, ["callback_answer", "message", "document"])
        shown = view_calls[1]["text"]
        self.assertIn(text("report.view.title", report_id=report.id), shown)
        self.assertIn(REPORT_TEXT, shown)
        self.assertIn("ID 7", shown)
        self.assertIsNone(view_calls[1].get("reply_markup"))
        self.assertIsNone(view_calls[2].get("reply_markup"))
        self.assertIn(
            f"События пользователя {DEFAULT_USER_ID}",
            _document_text(view_calls[2]),
        )

        # Репорт без событий за час: .txt получает короткую отметку каталога.
        with h.session_factory() as session:
            quiet = report_store.create_report(
                session,
                author_user_id=DEFAULT_USER_ID,
                chat_id=h.chat_id,
                body="Тихая жалоба",
                screen_label="main",
                events_snapshot=report_module._events_text(999_999),  # noqa: SLF001
                username=None,
                display_name="Тест",
            )
        before = len(h.calls)
        with patch("config.SUPERADMIN_USER_IDS", (DEFAULT_USER_ID,)):
            await h.callback(f"rp_view:{quiet.id}")
        quiet_calls = h.calls[before:]
        self.assertEqual(
            [call["type"] for call in quiet_calls],
            ["callback_answer", "message", "document"],
        )
        self.assertIn(text("report.events.empty"), _document_text(quiet_calls[2]))

        # Пустой журнал другого пользователя даёт короткую отметку каталога.
        report_module.SessionLocal = h.session_factory
        empty = report_module._events_text(999_999)  # noqa: SLF001
        self.assertIn("События пользователя 999999", empty)
        self.assertIn(text("report.events.empty"), empty)

        # `post_init` приложения — совмещённый hook: автоочистка анонсов и
        # список команд. Саму очистку здесь не зовём (она ходит в рабочую
        # фабрику `database.SessionLocal`), проверяется регистрация и
        # публикация единственной команды.
        self.assertIs(h.app.post_init, bot_module.post_init_application)
        await bot_module.post_init_set_my_commands(h.app)
        published = [call for call in h.calls if call["type"] == "set_my_commands"]
        self.assertEqual(len(published), 1)
        commands = published[0]["args"][0]
        self.assertEqual(
            [(command.command, command.description) for command in commands],
            [("report", text("report.command.description"))],
        )
        self.assertIsInstance(commands[0], BotCommand)

    @scenario_test
    async def test_block9_undelivered_notification_is_logged(self, h) -> None:
        """Сбой уведомления не теряет репорт и попадает в журнал."""
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID, OTHER_SUPERADMIN_ID)):
            await h.text("/start")
            h.bot.fail_types.add("message")
            await h.text("/report")
            await h.callback("rp_compose")
            await h.text(REPORT_TEXT)
            await h.callback("rp_send")
        h.bot.fail_types.discard("message")

        self.assertEqual(len(_reports(h)), 1)
        self.assertTrue(
            any(text("report.screen.sent") in shown for shown in h.screen_texts())
        )
        failed = [
            event
            for event in user_events.list_user_events(
                SUPERADMIN_ID, session_factory=h.session_factory
            )
            if event.action == "report.notification_failed"
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].result, "error")
        self.assertIn("report_id=1", failed[0].detail or "")
        self.assertIn("kind=message", failed[0].detail or "")
        self.assertEqual(_reports(h)[0].notified_user_ids, ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
