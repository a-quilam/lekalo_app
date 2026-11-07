# tests/test_stage255_reports_admin.py
"""
Poster 2.5.5, этап D — раздел «🛠 Репорты» административного кабинета.

Проверяется пользовательское требование, а не устройство реализации:
сценарии идут через настоящее приложение, FakeBot и временную SQLite
(`tests/integration_harness.py`). Рабочая `post_bot.db`, `.env` и сеть
Telegram не используются. Ожидания формулировок берутся из действующего
каталога `config/ui_texts.yaml` через `tests/ui_texts_expect.py`.

Что закреплено.

А. Вход и права:
- строка «🛠 Репорты» + счётчик действующих видна в меню кабинета ТОЛЬКО
  суперадмину; счётчик равен `count_active()`, кнопка счётчика — no-op;
- ошибка БД при подсчёте не ломает кабинет: меню рисуется без числа;
- обычный администратор площадки не видит строку и получает alert-отказ на
  любой `rpts_`-callback (проверка на каждом действии).

Б. Список и пагинация:
- кнопки «{screen_label} — {@username|имя|ID}», новые к репортам первыми;
- 6+ репортов → страница по 5 и строка `◀️ | 1/2 | ▶️`, clamp смещения;
- внизу «Архив» и «Назад» (в меню кабинета).

В. Карточка:
- автор, раздел, дата, полный текст; отдельным сообщением .txt-снимок;
- кнопки «Решено» и «Назад»;
- длинный текст не теряется: экран укладывается в лимит Telegram.

Г. Ответ («Решено»):
- шаг 1: «Напишите ответ пользователю.» + «Отмена»;
- шаг 2: текст обычным сообщением → предпросмотр
  «Отменить | Изменить | Отправить»; «Изменить» сохраняет текст,
  «Отменить» возвращает к карточке и репорт остаётся активным;
- шаг 3: сообщение автору «Спасибо за репорт! Ответ разработчика:…» на
  `reports.chat_id` с сохранением переносов; `archive_report` — только
  после подтверждённой доставки; при ошибке репорт активен, alert, повтор
  возможен; повторная отправка после успеха не дублирует сообщение.

Д. Два суперадмина:
- второй получает «репорт уже закрыт» и архивную карточку;
- статус перепроверяется перед отправкой.

Е. Архив:
- список по тем же правилам; карточка с ответом разработчика и датой
  закрытия; управление недоступно; возврат к активным и в кабинет.

Ж. Журнал:
- `admin_reports.list_opened / report_opened / reply_started /
  reply_cancelled / reply_sent / reply_failed / archive_opened`;
- в `detail` нет текста ответа.

Запуск из каталога Poster:
    .venv/bin/python -m unittest tests.test_stage255_reports_admin -v
"""

from __future__ import annotations

import unittest
from io import BytesIO
from unittest.mock import patch

from sqlalchemy import text as sqltext

import report_store

try:
    from tests import ui_texts_expect as cat
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.test_stage20_admin_flow import (
        callback_answers,
        insert_place,
        last_alert,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


SUPERADMIN_ID = 500
SECOND_SUPERADMIN_ID = 501
PLACE_ADMIN_ID = 9_115_001
AUTHOR_ID = 7_500_001
DENIED_ALERT = "У вас нет доступа к разделу администрирования."
REPORT_TEXT = "Нажал дату — календарь не открылся"
REPLY_TEXT = "Спасибо, поправили календарь"

STALE_ALERT = cat.text("errors.stale.admin")


# --- помощники -------------------------------------------------------------


def _seed_report(
    h,
    *,
    body: str = REPORT_TEXT,
    screen_label: str = "главное меню",
    author_user_id: int = AUTHOR_ID,
    username: str | None = "author",
    display_name: str | None = "Автор Репортов",
    chat_id: int | None = None,
    events: str = "2026-11-07 10:00 | screen.open | ok",
) -> int:
    """Строка репорта напрямую в БД (как её создаёт этап C)."""
    with h.session_factory() as session:
        report = report_store.create_report(
            session,
            author_user_id=author_user_id,
            chat_id=h.chat_id if chat_id is None else chat_id,
            body=body,
            screen_label=screen_label,
            events_snapshot=events,
            username=username,
            display_name=display_name,
        )
        return report.id


def _report(h, report_id: int, *, tolerate_missing: bool = False):
    with h.session_factory() as session:
        report = report_store.get_report(session, report_id)
    if report is None and not tolerate_missing:
        raise AssertionError(f"репорт {report_id} не найден")
    return report


def _documents(h) -> list[dict]:
    return [call for call in h.calls if call["type"] == "document"]


def _document_text(record: dict) -> str:
    document = record.get("document")
    if isinstance(document, BytesIO):
        return document.getvalue().decode("utf-8")
    raise AssertionError(f"вложение не BytesIO: {document!r}")


def _messages_to(h, chat_id: int) -> list[dict]:
    return [
        call
        for call in h.calls
        if call["type"] == "message" and call.get("chat_id") == chat_id
    ]


def _events(h, action: str) -> list[tuple]:
    with h.session_factory() as session:
        return session.execute(
            sqltext(
                "SELECT telegram_user_id, screen, result, detail FROM user_event_log "
                "WHERE action = :action ORDER BY id"
            ),
            {"action": action},
        ).all()


def _callbacks(h) -> list[str | None]:
    return [data for _label, data in h.screen_buttons()]


def _labels(h) -> list[str]:
    return [label for label, _data in h.screen_buttons()]


class ReportsBase(unittest.TestCase):
    """Базовый класс: оба суперадмина активны на время сценария."""

    def setUp(self) -> None:
        patcher = patch(
            "config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID, SECOND_SUPERADMIN_ID)
        )
        patcher.start()
        self.addCleanup(patcher.stop)


async def _open_reports(h, *, user_id: int = SUPERADMIN_ID) -> None:
    """Кабинет → «🛠 Репорты»."""
    await h.text("/admin", user_id=user_id)
    await h.callback("rpts_list", user_id=user_id)


async def _open_card(h, report_id: int, *, user_id: int = SUPERADMIN_ID) -> None:
    await _open_reports(h, user_id=user_id)
    await h.callback(f"rpts_card:{report_id}", user_id=user_id)


async def _start_reply(h, report_id: int, *, user_id: int = SUPERADMIN_ID) -> None:
    await _open_card(h, report_id, user_id=user_id)
    await h.callback(f"rpts_reply:{report_id}", user_id=user_id)


# --- А. Вход, счётчик, права ----------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReportsEntryTests(ReportsBase):
    """Вход в раздел: только суперадмин, счётчик действующих репортов."""

    @scenario_test
    async def test_superadmin_sees_entry_with_counter(self, h) -> None:
        insert_place(h, "Кофейня")
        _seed_report(h)
        _seed_report(h, body="Вторая жалоба", username=None, display_name=None)

        await h.text("/admin", user_id=SUPERADMIN_ID)

        labels = _labels(h)
        self.assertIn(cat.text("buttons.reports.open"), labels)
        self.assertIn(
            cat.text("buttons.reports.counter", count=2),
            labels,
            f"нет счётчика действующих репортов: {labels}",
        )
        # Счётчик — неактивная кнопка с безопасным no-op callback.
        counter = [
            data
            for label, data in h.screen_buttons()
            if label == cat.text("buttons.reports.counter", count=2)
        ]
        self.assertEqual(len(counter), 1)
        self.assertTrue(counter[0].startswith("noop:"))

        # Нажатие счётчика ничего не меняет: спиннер закрыт, экран тот же.
        screen_before = h.screen_text()
        before_calls = len(h.calls)
        await h.callback(counter[0], user_id=SUPERADMIN_ID)
        self.assertEqual(h.screen_text(), screen_before)
        self.assertEqual(
            [call["type"] for call in h.calls[before_calls:]],
            ["callback_answer"],
        )

    @scenario_test
    async def test_counter_failure_does_not_break_cabinet(self, h) -> None:
        insert_place(h, "Кофейня")
        with patch.object(
            report_store,
            "count_active",
            side_effect=RuntimeError("база недоступна"),
        ):
            await h.text("/admin", user_id=SUPERADMIN_ID)

        labels = _labels(h)
        self.assertIn(cat.text("buttons.reports.open"), labels)
        for label in labels:
            self.assertNotIn(
                cat.fragment("buttons.reports.counter"),
                label,
                f"счётчик показан при ошибке БД: {label}",
            )
        self.assertTrue(h.screen_text())

    @scenario_test
    async def test_place_admin_has_no_entry_and_is_denied(self, h) -> None:
        place_id = insert_place(h, "Своё место")
        report_id = _seed_report(h)
        with h.session_factory() as session:
            from sqlalchemy import select

            from models import PlaceAdmin, User

            user = session.scalar(
                select(User).where(User.telegram_user_id == PLACE_ADMIN_ID)
            )
            if user is None:
                from user_store import get_or_create_user

                user = get_or_create_user(session, PLACE_ADMIN_ID, username="pa")
            session.add(PlaceAdmin(place_id=place_id, user_id=user.id))
            session.commit()

        await h.text("/admin", user_id=PLACE_ADMIN_ID)
        labels = _labels(h)
        self.assertNotIn(cat.text("buttons.reports.open"), labels)
        self.assertNotIn("rpts_list", _callbacks(h))

        # Подделанные callback — отказ на КАЖДОМ действии раздела.
        for data in (
            "rpts_list",
            f"rpts_card:{report_id}",
            "rpts_archive",
            f"rpts_acard:{report_id}",
            f"rpts_reply:{report_id}",
            "rpts_reply_change",
            "rpts_reply_cancel",
            "rpts_reply_send",
            "rpts_back",
            "rpts_cabinet",
            "rpts_page:0",
            "rpts_archive_page:0",
        ):
            with self.subTest(data=data):
                before = len(callback_answers(h))
                await h.callback(data, user_id=PLACE_ADMIN_ID)
                self.assertGreater(len(callback_answers(h)), before)
                self.assertEqual(last_alert(h), DENIED_ALERT)
        # Репорт не закрыт и ответ не отправлен.
        self.assertTrue(_report(h, report_id).is_active)


# --- Б. Список и пагинация -------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReportsListTests(ReportsBase):
    """Список действующих репортов: подписи, порядок, пагинация, возвраты."""

    @scenario_test
    async def test_list_items_order_and_labels(self, h) -> None:
        first = _seed_report(h, screen_label="конструктор анонса")
        second = _seed_report(
            h,
            body="Второй",
            screen_label="шаблоны",
            username=None,
            display_name="Без Ника",
            author_user_id=AUTHOR_ID + 1,
        )
        third = _seed_report(
            h,
            body="Третий",
            screen_label="места",
            username=None,
            display_name=None,
            author_user_id=AUTHOR_ID + 2,
        )

        await _open_reports(h)

        cat.assert_shows(self, h.screen_text(), "admin.reports.active_total", total=3)
        buttons = h.screen_buttons()
        report_rows = [
            (label, data)
            for label, data in buttons
            if data and data.startswith("rpts_card:")
        ]
        # Новые к старым: последний созданный — первым.
        self.assertEqual(
            [data for _label, data in report_rows],
            [f"rpts_card:{third}", f"rpts_card:{second}", f"rpts_card:{first}"],
        )
        # Подпись: раздел — @username | имя | ID.
        self.assertEqual(
            report_rows[0][0],
            cat.text(
                "buttons.reports.item",
                screen_label="места",
                author_label=str(AUTHOR_ID + 2),
            ),
        )
        self.assertIn("Без Ника", report_rows[1][0])
        self.assertIn("@author", report_rows[2][0])
        # Внизу «Архив» и «Назад» в меню кабинета.
        self.assertEqual(
            _labels(h)[-2:],
            [cat.text("buttons.reports.archive"), cat.text("buttons.nav.back")],
        )
        self.assertEqual(_callbacks(h)[-2:], ["rpts_archive", "rpts_cabinet"])
        # Пагинации нет: записей меньше страницы.
        self.assertFalse([d for d in _callbacks(h) if d and d.startswith("rpts_page:")])

    @scenario_test
    async def test_pagination_and_clamp(self, h) -> None:
        for index in range(7):
            _seed_report(h, body=f"Жалоба {index}")

        await _open_reports(h)

        page_rows = [d for d in _callbacks(h) if d and d.startswith("rpts_card:")]
        self.assertEqual(len(page_rows), 5, f"страница не по пять: {page_rows}")
        self.assertIn("1/2", _labels(h))
        self.assertNotIn("◀️", _labels(h))

        await h.callback("rpts_page:5", user_id=SUPERADMIN_ID)
        second = [d for d in _callbacks(h) if d and d.startswith("rpts_card:")]
        self.assertEqual(len(second), 2)
        self.assertIn("2/2", _labels(h))

        # Смещение за пределами списка приводит к последней странице, а не к
        # пустому экрану.
        await h.callback("rpts_page:100", user_id=SUPERADMIN_ID)
        self.assertEqual(
            [d for d in _callbacks(h) if d and d.startswith("rpts_card:")], second
        )

    @scenario_test
    async def test_back_returns_to_cabinet_menu(self, h) -> None:
        insert_place(h, "Кофейня")
        _seed_report(h)
        await _open_reports(h)

        await h.callback("rpts_cabinet", user_id=SUPERADMIN_ID)
        cat.assert_shows(self, h.screen_text(), "admin.places.vse_mesta")
        self.assertIn(
            cat.text("buttons.reports.open"),
            _labels(h),
            "в меню кабинета нет входа в «Репорты»",
        )


# --- В. Карточка -----------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReportCardTests(ReportsBase):
    """Карточка действующего репорта: данные, .txt и кнопки."""

    @scenario_test
    async def test_card_shows_data_and_snapshot(self, h) -> None:
        report_id = _seed_report(h, events="2026-11-07 10:00 | screen.open | ok")
        await _open_card(h, report_id)

        text = h.screen_text()
        cat.assert_shows(self, text, "admin.reports.card_title", report_id=report_id)
        cat.assert_shows(self, text, "admin.reports.author", author_label="@author")
        cat.assert_shows(self, text, "admin.reports.section")
        cat.assert_shows(self, text, "admin.reports.created")
        cat.assert_shows(self, text, "admin.reports.body", body=REPORT_TEXT)
        self.assertIn("главное меню", text)

        self.assertEqual(
            _callbacks(h),
            [f"rpts_reply:{report_id}", "rpts_back"],
        )
        self.assertEqual(
            _labels(h),
            [cat.text("buttons.reports.resolved"), cat.text("buttons.nav.back")],
        )

        documents = _documents(h)
        self.assertEqual(len(documents), 1, "нет .txt-снимка отдельным сообщением")
        self.assertEqual(
            _document_text(documents[0]),
            "2026-11-07 10:00 | screen.open | ok",
        )
        self.assertTrue(
            str(documents[0].get("filename") or "").startswith("report_"),
            documents[0].get("filename"),
        )

    @scenario_test
    async def test_long_body_fits_screen_without_losing_data(self, h) -> None:
        body = "Ж" * 3900
        report_id = _seed_report(h, body=body)
        await _open_card(h, report_id)

        screen = h.screen_text()
        self.assertLessEqual(len(screen), 4096, "экран длиннее лимита Telegram")
        cat.assert_shows(self, screen, "admin.reports.card_title", report_id=report_id)
        # В базе текст не изменился.
        self.assertEqual(_report(h, report_id).body, body)
        # .txt-снимок отправлен.
        self.assertEqual(len(_documents(h)), 1)

    @scenario_test
    async def test_missing_report_alerts(self, h) -> None:
        await _open_reports(h)
        await h.callback("rpts_card:999999", user_id=SUPERADMIN_ID)
        self.assertEqual(last_alert(h), cat.text("admin.reports.missing"))


# --- Г. Ответ пользователю -------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReportReplyTests(ReportsBase):
    """Машина состояний ответа: ввод, предпросмотр, отправка."""

    @scenario_test
    async def test_reply_sends_to_author_and_archives(self, h) -> None:
        report_id = _seed_report(h)
        await _start_reply(h, report_id)

        cat.assert_shows(self, h.screen_text(), "admin.reports.reply_prompt")
        self.assertEqual(_callbacks(h), ["rpts_reply_cancel"])

        await h.text(REPLY_TEXT, user_id=SUPERADMIN_ID)
        cat.assert_shows(
            self, h.screen_text(), "admin.reports.reply_preview", reply=REPLY_TEXT
        )
        self.assertEqual(
            _callbacks(h), ["rpts_reply_cancel", "rpts_reply_change", "rpts_reply_send"]
        )
        # Репорт ещё активен: отправки не было.
        self.assertTrue(_report(h, report_id).is_active)

        before = len(_messages_to(h, h.chat_id))
        await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)

        # Сообщение автору — на chat_id репорта, с текстом каталога.
        delivered = _messages_to(h, h.chat_id)[before:]
        self.assertEqual(len(delivered), 1, f"нет ответа автору: {delivered}")
        self.assertEqual(
            delivered[0]["text"],
            cat.text("admin.reports.user_reply", reply=REPLY_TEXT),
        )
        self.assertIn("\n\n", delivered[0]["text"], "переносы ответа потеряны")

        archived = _report(h, report_id)
        self.assertTrue(archived.is_archived)
        self.assertEqual(archived.developer_reply, REPLY_TEXT)
        self.assertIsNotNone(archived.closed_at)

        # Экран после успеха — архивная карточка, действий управления нет.
        cat.assert_shows(
            self, h.screen_text(), "admin.reports.reply_label", reply=REPLY_TEXT
        )
        self.assertNotIn("rpts_reply_send", _callbacks(h))
        self.assertEqual(_events(h, "admin_reports.reply_sent")[0][0], SUPERADMIN_ID)

    @scenario_test
    async def test_multiline_reply_keeps_line_breaks(self, h) -> None:
        report_id = _seed_report(h)
        await _start_reply(h, report_id)
        await h.text("Первая строка\nВторая строка", user_id=SUPERADMIN_ID)
        await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)

        delivered = [
            call
            for call in _messages_to(h, h.chat_id)
            if cat.fragment("admin.reports.user_reply") in (call.get("text") or "")
        ]
        self.assertEqual(len(delivered), 1)
        self.assertIn("Первая строка\nВторая строка", delivered[0]["text"])

    @scenario_test
    async def test_change_keeps_text_and_cancel_returns_to_card(self, h) -> None:
        report_id = _seed_report(h)
        await _start_reply(h, report_id)
        await h.text("Первый вариант", user_id=SUPERADMIN_ID)

        await h.callback("rpts_reply_change", user_id=SUPERADMIN_ID)
        cat.assert_shows(self, h.screen_text(), "admin.reports.reply_prompt_change")

        # Новый текст заменяет предыдущий.
        await h.text("Второй вариант", user_id=SUPERADMIN_ID)
        cat.assert_shows(
            self, h.screen_text(), "admin.reports.reply_preview", reply="Второй вариант"
        )

        # «Отменить» возвращает к карточке; репорт остаётся активным.
        await h.callback("rpts_reply_cancel", user_id=SUPERADMIN_ID)
        cat.assert_shows(
            self, h.screen_text(), "admin.reports.card_title", report_id=report_id
        )
        self.assertEqual(_callbacks(h), [f"rpts_reply:{report_id}", "rpts_back"])
        report = _report(h, report_id)
        self.assertTrue(report.is_active)
        self.assertIsNone(report.developer_reply)
        self.assertEqual(
            _events(h, "admin_reports.reply_cancelled")[0][0], SUPERADMIN_ID
        )
        # Сообщение пользователю не отправлялось.
        self.assertFalse(
            [
                call
                for call in _messages_to(h, h.chat_id)
                if call.get("chat_id") == h.chat_id
                and cat.fragment("admin.reports.user_reply") in (call.get("text") or "")
            ]
        )

    @scenario_test
    async def test_reply_input_only_active_in_own_phase(self, h) -> None:
        _seed_report(h)
        await _open_reports(h)
        # Вне фазы ввода обычный текст не перехватывается разделом репортов.
        before = h.screen_text()
        await h.text("Обычный текст", user_id=SUPERADMIN_ID)
        self.assertNotEqual(h.screen_text(), before)

    @scenario_test
    async def test_empty_reply_is_refused(self, h) -> None:
        report_id = _seed_report(h)
        await _start_reply(h, report_id)
        await h.text("   ", user_id=SUPERADMIN_ID)
        cat.assert_shows(self, h.screen_text(), "admin.reports.reply_empty")
        self.assertEqual(_callbacks(h), ["rpts_reply_cancel"])
        self.assertTrue(_report(h, report_id).is_active)

    @scenario_test
    async def test_delivery_failure_keeps_report_active_and_allows_retry(
        self, h
    ) -> None:
        report_id = _seed_report(h)
        await _start_reply(h, report_id)
        await h.text(REPLY_TEXT, user_id=SUPERADMIN_ID)

        h.bot.fail_types.add("message")
        try:
            await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)
        finally:
            h.bot.fail_types.discard("message")

        self.assertEqual(last_alert(h), cat.text("admin.reports.reply_failed"))
        report = _report(h, report_id)
        self.assertTrue(report.is_active, "репорт закрыт без доставки")
        self.assertIsNone(report.developer_reply)
        self.assertEqual(_events(h, "admin_reports.reply_failed")[0][2], "error")

        # Повтор возможен: текст ответа сохранён в состоянии.
        await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)
        self.assertTrue(_report(h, report_id).is_archived)
        self.assertEqual(_report(h, report_id).developer_reply, REPLY_TEXT)

    @scenario_test
    async def test_repeat_send_does_not_duplicate(self, h) -> None:
        report_id = _seed_report(h)
        await _start_reply(h, report_id)
        await h.text(REPLY_TEXT, user_id=SUPERADMIN_ID)
        await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)

        delivered_before = [
            call
            for call in _messages_to(h, h.chat_id)
            if cat.fragment("admin.reports.user_reply") in (call.get("text") or "")
        ]
        self.assertEqual(len(delivered_before), 1)

        # Повторная отправка (двойной клик/повтор update) ничего не шлёт.
        await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)
        delivered_after = [
            call
            for call in _messages_to(h, h.chat_id)
            if cat.fragment("admin.reports.user_reply") in (call.get("text") or "")
        ]
        self.assertEqual(len(delivered_after), 1, "ответ продублирован")
        self.assertTrue(_report(h, report_id).is_archived)

    @scenario_test
    async def test_long_reply_delivered_in_parts_without_loss(self, h) -> None:
        """Длинный ответ уходит частями: ни один символ не теряется."""
        report_id = _seed_report(h)
        reply = "<" * 3400  # после экранирования — 13 600 символов HTML
        await _start_reply(h, report_id)
        await h.text(reply, user_id=SUPERADMIN_ID)
        self.assertLessEqual(len(h.screen_text()), 4096)

        before = len(h.calls)
        await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)
        parts = [
            call.get("text") or ""
            for call in h.calls[before:]
            if call["type"] == "message" and call.get("chat_id") == h.chat_id
        ]
        self.assertGreater(len(parts), 1, "длинный ответ отправлен одной частью")
        for part in parts:
            self.assertLessEqual(len(part), 4000)
        self.assertEqual("".join(parts).count("&lt;"), len(reply))
        self.assertEqual(_report(h, report_id).developer_reply, reply)

    @scenario_test
    async def test_db_failure_after_delivery_does_not_resend(self, h) -> None:
        """Сбой записи после доставки: повтор не дублирует сообщение автору."""
        report_id = _seed_report(h)
        await _start_reply(h, report_id)
        await h.text(REPLY_TEXT, user_id=SUPERADMIN_ID)

        with patch.object(
            report_store,
            "archive_report",
            side_effect=RuntimeError("база недоступна"),
        ):
            await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)
        self.assertEqual(last_alert(h), cat.text("admin.reports.reply_failed"))
        # Автору ответ ушёл, но репорт не закрыт.
        self.assertTrue(_report(h, report_id).is_active)

        await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)
        delivered = [
            call
            for call in _messages_to(h, h.chat_id)
            if cat.fragment("admin.reports.user_reply") in (call.get("text") or "")
        ]
        self.assertEqual(len(delivered), 1, "ответ продублирован после сбоя БД")
        archived = _report(h, report_id)
        self.assertTrue(archived.is_archived)
        self.assertEqual(archived.developer_reply, REPLY_TEXT)

    @scenario_test
    async def test_partial_delivery_retry_sends_only_missing_parts(self, h) -> None:
        """Сбой на второй части: повтор дошлёт только недостающее."""
        report_id = _seed_report(h)
        reply = "<" * 3400  # после экранирования — 13 600 символов HTML
        await _start_reply(h, report_id)
        await h.text(reply, user_id=SUPERADMIN_ID)

        before = len(h.calls)
        h.bot.fail_after["message"] = 1  # первая часть уходит, вторая падает
        try:
            await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)
        finally:
            h.bot.fail_after.pop("message", None)

        self.assertEqual(last_alert(h), cat.text("admin.reports.reply_failed"))
        first_wave = [
            call.get("text") or ""
            for call in h.calls[before:]
            if call["type"] == "message" and call.get("chat_id") == h.chat_id
        ]
        self.assertEqual(len(first_wave), 1, "первая часть не доставлена")
        self.assertIn(cat.fragment("admin.reports.user_reply"), first_wave[0])
        self.assertTrue(_report(h, report_id).is_active)

        before = len(h.calls)
        await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)
        retry_wave = [
            call.get("text") or ""
            for call in h.calls[before:]
            if call["type"] == "message" and call.get("chat_id") == h.chat_id
        ]
        self.assertTrue(retry_wave, "повтор ничего не отправил")
        self.assertTrue(_report(h, report_id).is_archived)

        all_parts = first_wave + retry_wave
        header = cat.fragment("admin.reports.user_reply")
        self.assertEqual(
            sum(part.count(header) for part in all_parts),
            1,
            "заголовок «Спасибо за репорт!» продублирован",
        )
        self.assertEqual(
            "".join(all_parts).count("&lt;"),
            len(reply),
            "части ответа потеряны или продублированы",
        )

    @scenario_test
    async def test_reply_with_html_characters_is_escaped(self, h) -> None:
        """`<`, `>` и `&` в ответе — текст, а не разметка."""
        report_id = _seed_report(h)
        reply = "Формула: a < b && c > d"
        await _start_reply(h, report_id)
        await h.text(reply, user_id=SUPERADMIN_ID)

        preview = h.screen_text()
        self.assertIn("a &lt; b", preview)
        self.assertNotIn("<b", preview)

        await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)
        delivered = [
            call
            for call in _messages_to(h, h.chat_id)
            if call.get("chat_id") == h.chat_id
            and "Ответ разработчика" in (call.get("text") or "")
        ][-1]
        self.assertIn("a &lt; b", delivered["text"])
        self.assertIn("&amp;&amp;", delivered["text"])
        self.assertEqual(_report(h, report_id).developer_reply, reply)

    @scenario_test
    async def test_journal_has_no_reply_text(self, h) -> None:
        report_id = _seed_report(h)
        await _start_reply(h, report_id)
        await h.text(REPLY_TEXT, user_id=SUPERADMIN_ID)
        await h.callback("rpts_reply_send", user_id=SUPERADMIN_ID)

        for action in (
            "admin_reports.report_opened",
            "admin_reports.reply_started",
            "admin_reports.reply_sent",
        ):
            rows = _events(h, action)
            self.assertTrue(rows, f"нет события {action}")
            for _user, screen, _result, detail in rows:
                self.assertEqual(screen, "admin_reports")
                self.assertNotIn(REPLY_TEXT, detail or "")
                self.assertNotIn(REPORT_TEXT, detail or "")


# --- Д. Два суперадмина ----------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReportsConcurrencyTests(ReportsBase):
    """Второй суперадмин видит закрытый репорт и не может ответить."""

    @scenario_test
    async def test_second_superadmin_gets_closed_notice_and_archive_card(
        self, h
    ) -> None:
        report_id = _seed_report(h)

        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID, SECOND_SUPERADMIN_ID)):
            # Первый открывает карточку, но ещё не отвечает.
            await _open_card(h, report_id, user_id=SECOND_SUPERADMIN_ID)

            # Первый (другой) закрывает репорт напрямую.
            with h.session_factory() as session:
                report_store.archive_report(
                    session, report_id, reply="Закрыто в другом окне"
                )

            # Второй жмёт «Решено» — статус перепроверяется.
            await h.callback(f"rpts_reply:{report_id}", user_id=SECOND_SUPERADMIN_ID)
            self.assertEqual(last_alert(h), cat.text("admin.reports.already_closed"))
            cat.assert_shows(
                self,
                h.screen_text(),
                "admin.reports.reply_label",
                reply="Закрыто в другом окне",
            )
            self.assertNotIn("rpts_reply_send", _callbacks(h))

    @scenario_test
    async def test_send_after_external_close_is_refused(self, h) -> None:
        report_id = _seed_report(h)
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_ID, SECOND_SUPERADMIN_ID)):
            await _start_reply(h, report_id, user_id=SECOND_SUPERADMIN_ID)
            await h.text(REPLY_TEXT, user_id=SECOND_SUPERADMIN_ID)

            with h.session_factory() as session:
                report_store.archive_report(session, report_id, reply="Уже закрыт")

            before = len(_messages_to(h, h.chat_id))
            await h.callback("rpts_reply_send", user_id=SECOND_SUPERADMIN_ID)
            self.assertEqual(last_alert(h), cat.text("admin.reports.already_closed"))
            delivered = [
                call
                for call in _messages_to(h, h.chat_id)[before:]
                if cat.fragment("admin.reports.user_reply") in (call.get("text") or "")
            ]
            self.assertFalse(delivered, "ответ отправлен в закрытый репорт")
            self.assertEqual(_report(h, report_id).developer_reply, "Уже закрыт")
            # Экран после отказа — архивная карточка, а не ввод ответа:
            # иначе повторный ввод упирался бы в тот же отказ по кругу.
            screen = h.screen_text()
            cat.assert_shows(
                self, screen, "admin.reports.reply_label", reply="Уже закрыт"
            )
            cat.assert_not_shows(self, screen, "admin.reports.reply_prompt")
            self.assertNotIn("rpts_reply_send", _callbacks(h))


# --- Е. Архив --------------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ReportsArchiveTests(ReportsBase):
    """Архив: список, карточка с ответом, возвраты, недоступность управления."""

    @scenario_test
    async def test_archive_list_and_card(self, h) -> None:
        closed_id = _seed_report(h, screen_label="места")
        _seed_report(h, body="Ещё активный")
        with h.session_factory() as session:
            report_store.archive_report(session, closed_id, reply="Исправлено")

        await _open_reports(h)
        await h.callback("rpts_archive", user_id=SUPERADMIN_ID)

        cat.assert_shows(self, h.screen_text(), "admin.reports.archive_title")
        self.assertEqual(
            _callbacks(h),
            [f"rpts_acard:{closed_id}", "rpts_back", "rpts_cabinet"],
        )
        self.assertEqual(
            _labels(h),
            [
                cat.text(
                    "buttons.reports.item",
                    screen_label="места",
                    author_label="@author",
                ),
                cat.text("buttons.nav.back"),
                cat.text("buttons.reports.cabinet"),
            ],
        )
        self.assertEqual(
            _events(h, "admin_reports.archive_opened")[0][0], SUPERADMIN_ID
        )

        await h.callback(f"rpts_acard:{closed_id}", user_id=SUPERADMIN_ID)
        text = h.screen_text()
        cat.assert_shows(self, text, "admin.reports.card_title", report_id=closed_id)
        cat.assert_shows(self, text, "admin.reports.reply_label", reply="Исправлено")
        cat.assert_shows(self, text, "admin.reports.closed")
        cat.assert_shows(self, text, "admin.reports.body", body=REPORT_TEXT)
        # Управление недоступно: только возврат в архив.
        self.assertEqual(_callbacks(h), ["rpts_archive"])
        self.assertEqual(len(_documents(h)), 1)

    @scenario_test
    async def test_archive_empty_state_and_back(self, h) -> None:
        _seed_report(h)
        await _open_reports(h)
        await h.callback("rpts_archive", user_id=SUPERADMIN_ID)
        cat.assert_shows(self, h.screen_text(), "admin.reports.archive_empty")
        self.assertIn("rpts_back", _callbacks(h))

        await h.callback("rpts_back", user_id=SUPERADMIN_ID)
        cat.assert_shows(self, h.screen_text(), "admin.reports.active_total", total=1)

    @scenario_test
    async def test_active_report_not_opened_from_archive(self, h) -> None:
        active_id = _seed_report(h)
        await _open_reports(h)
        await h.callback("rpts_archive", user_id=SUPERADMIN_ID)
        await h.callback(f"rpts_acard:{active_id}", user_id=SUPERADMIN_ID)
        self.assertEqual(last_alert(h), cat.text("admin.reports.missing"))
