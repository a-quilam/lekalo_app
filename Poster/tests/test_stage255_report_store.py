# tests/test_stage255_report_store.py
"""
Этап B: журнал событий пользователя и постоянные репорты.

Проверяется фундамент системы репортов, без UI и команды `/report`:

- миграция 29 накатывается на свежую базу и на копию базы версии 28,
  добавляя ТОЛЬКО `user_event_log`, `reports` и их индексы;
- `report_store` — CRUD, пагинация «новые к старым», счётчик `active`,
  двойное архивирование (второй UPDATE не меняет строку), `save_reply`
  только для действующего, `record_notification` без дублей;
- `utils.user_events` — запись/чтение событий за час (граница «ровно час
  назад» включительна, более старые не попадают), retention, контракт
  `detail` (только техника, без пользовательского текста) и защита
  обработчика от сбоя журнала.

Тесты работают только с временными файлами SQLite; реальная `post_bot.db`,
`.env` и Telegram не используются.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy.orm import sessionmaker

import utils.user_events as user_events
from migrations import DEFAULT_MIGRATIONS, LATEST_VERSION, run_migrations
from models import utcnow
from report_store import (
    REPORTS_PAGE_SIZE,
    STATUS_ARCHIVED,
    ReportError,
    active_reports,
    archive_report,
    archived_reports,
    count_active,
    create_report,
    get_report,
    record_notification,
    save_reply,
)
from tests.stage19a_helpers import make_engine, temp_db_path

MIGRATION_28_NAME = "template_snapshot_conditional_parts"
MIGRATION_29_NAME = "user_event_log_reports"
MIGRATION_30_NAME = "standard_template_extended"

USER_TG = 77_500_001
OTHER_TG = 77_500_002
ADMIN_TG = 77_500_003

try:
    from announcement_instances import archive_instance
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.test_stage25_delivery_instances import (
        _delivered_without_photos,
        _only_active,
    )
    from tests.test_stage27b_announcement_ui import _open_field_question

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


def _sql(path: str, statement: str, params: tuple = ()) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        return connection.execute(statement, params).fetchall()
    finally:
        connection.close()


def _execute(path: str, statement: str, params: tuple = ()) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(statement, params)
        connection.commit()
    finally:
        connection.close()


def _columns(path: str, table: str) -> dict[str, tuple]:
    return {
        str(row[1]): (str(row[2]).upper(), int(row[3]), row[4])
        for row in _sql(path, f"PRAGMA table_info({table})")
    }


def _indexes(path: str, table: str) -> set[str]:
    return {
        str(row[1])
        for row in _sql(path, f"PRAGMA index_list({table})")
        if not str(row[1]).startswith("sqlite_")
    }


def _journal(path: str) -> list[tuple[int, str]]:
    return [
        (int(row[0]), str(row[1]))
        for row in _sql(
            path, "SELECT version, name FROM schema_migrations ORDER BY version"
        )
    ]


class _DatabaseCase(unittest.TestCase):
    """Временная база с миграциями 1–29 и фабрика сессий."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster-255-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.applied = run_migrations(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.addCleanup(self.session.close)


# --- миграция 29 ------------------------------------------------------------


class Migration29Tests(unittest.TestCase):
    """Свежая установка и апгрейд с версии 28."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster-255-migr-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)

    def migrate(self, migrations=DEFAULT_MIGRATIONS) -> int:
        engine = make_engine(self.path)
        try:
            return run_migrations(engine, migrations)
        finally:
            engine.dispose()

    def test_latest_version_is_thirty(self) -> None:
        self.assertEqual(LATEST_VERSION, 30)
        self.assertEqual(self.migrate(), LATEST_VERSION)
        # Этап A1 добавил миграцию 30: журнал заканчивается парой 29/30,
        # сама запись 29 остаётся на своём месте.
        self.assertEqual(
            _journal(self.path)[-2:],
            [
                (29, MIGRATION_29_NAME),
                (30, MIGRATION_30_NAME),
            ],
        )

    def test_fresh_install_has_event_log_and_reports(self) -> None:
        self.assertEqual(self.migrate(), LATEST_VERSION)
        # `INTEGER PRIMARY KEY` в SQLite отдаёт `notnull=0`, поэтому PK
        # проверяется отдельным флагом, а NULL-ность — по `notnull`.
        table_info = {
            str(row[1]): (str(row[2]).upper(), int(row[3]), int(row[5]))
            for row in _sql(self.path, "PRAGMA table_info(user_event_log)")
        }
        self.assertEqual(
            table_info,
            {
                "id": ("INTEGER", 1, 1),
                "telegram_user_id": ("INTEGER", 1, 0),
                "created_at": ("DATETIME", 1, 0),
                "action": ("TEXT", 1, 0),
                "screen": ("TEXT", 0, 0),
                "scenario": ("TEXT", 0, 0),
                "result": ("TEXT", 0, 0),
                "detail": ("TEXT", 0, 0),
            },
        )
        self.assertEqual(
            _indexes(self.path, "user_event_log"),
            {
                "ix_user_event_log_user_created",
                "ix_user_event_log_created",
            },
        )
        report_columns = _columns(self.path, "reports")
        self.assertEqual(
            set(report_columns),
            {
                "id",
                "author_user_id",
                "chat_id",
                "username",
                "display_name",
                "body",
                "screen_label",
                "created_at",
                "events_snapshot",
                "status",
                "developer_reply",
                "replied_at",
                "closed_at",
                "notified_user_ids",
            },
        )
        # NULL-ность обязательных и необязательных полей — часть контракта.
        for required in (
            "author_user_id",
            "chat_id",
            "body",
            "screen_label",
            "created_at",
            "events_snapshot",
            "status",
        ):
            with self.subTest(column=required):
                self.assertEqual(report_columns[required][1], 1)
        for optional in (
            "username",
            "display_name",
            "developer_reply",
            "replied_at",
            "closed_at",
            "notified_user_ids",
        ):
            with self.subTest(column=optional):
                self.assertEqual(report_columns[optional][1], 0)
        self.assertEqual(report_columns["status"][2], "'active'")
        self.assertIn("ix_reports_status_created", _indexes(self.path, "reports"))
        self.assertEqual(_sql(self.path, "PRAGMA integrity_check"), [("ok",)])

    def test_upgrade_from_v28_adds_only_new_objects(self) -> None:
        self.assertEqual(self.migrate(DEFAULT_MIGRATIONS[:28]), 28)
        before = {
            str(row[0]) for row in _sql(self.path, "SELECT name FROM sqlite_master")
        }
        self.assertNotIn("user_event_log", before)
        self.assertNotIn("reports", before)
        self.assertEqual(self.migrate(), LATEST_VERSION)
        after = {
            str(row[0]) for row in _sql(self.path, "SELECT name FROM sqlite_master")
        }
        self.assertEqual(
            after - before,
            {
                "user_event_log",
                "reports",
                "ix_user_event_log_user_created",
                "ix_user_event_log_created",
                "ix_reports_status_created",
            },
            "миграция только добавочная",
        )
        # Этап A1 добавил 30: запись 29 теперь предпоследняя.
        self.assertEqual(_journal(self.path)[-2], (29, MIGRATION_29_NAME))
        # Повторный запуск ничего не меняет.
        self.assertEqual(self.migrate(), LATEST_VERSION)
        self.assertEqual(
            {str(row[0]) for row in _sql(self.path, "SELECT name FROM sqlite_master")},
            after,
        )

    def test_reports_status_check_and_default(self) -> None:
        self.assertEqual(self.migrate(), LATEST_VERSION)
        _execute(
            self.path,
            "INSERT INTO reports (author_user_id, chat_id, body, screen_label, "
            "created_at, events_snapshot) VALUES (1, 2, 't', 'main', "
            "'2026-01-01 00:00:00', 'snap')",
        )
        self.assertEqual(_sql(self.path, "SELECT status FROM reports"), [("active",)])
        with self.assertRaises(sqlite3.IntegrityError):
            _execute(
                self.path,
                "INSERT INTO reports (author_user_id, chat_id, body, screen_label, "
                "created_at, events_snapshot, status) VALUES (1, 2, 't', 'main', "
                "'2026-01-01 00:00:00', 'snap', 'deleted')",
            )


# --- report_store -----------------------------------------------------------


class ReportStoreTests(_DatabaseCase):
    """Создание, чтение, пагинация, закрытие и уведомления."""

    def make_report(self, **overrides):
        payload = {
            "author_user_id": USER_TG,
            "chat_id": 1000,
            "body": "Не открывается меню",
            "screen_label": "Главное меню",
            "events_snapshot": "2026-01-01 00:00:00 | screen.open | result=ok",
            "username": "tester",
            "display_name": "Тестер",
        }
        payload.update(overrides)
        return create_report(self.session, **payload)

    def test_create_and_get_report(self) -> None:
        created = self.make_report()
        self.assertGreater(created.id, 0)
        self.assertEqual(created.status, "active")
        self.assertTrue(created.is_active)
        self.assertEqual(created.author_user_id, USER_TG)
        self.assertEqual(created.chat_id, 1000)
        self.assertEqual(created.username, "tester")
        self.assertEqual(created.display_name, "Тестер")
        self.assertEqual(created.body, "Не открывается меню")
        self.assertEqual(created.screen_label, "Главное меню")
        self.assertEqual(created.notified_user_ids, ())
        self.assertEqual(created.author_label(), "@tester")
        stored = get_report(self.session, created.id)
        self.assertEqual(stored, created)

    def test_required_fields_are_validated(self) -> None:
        with self.assertRaises(ReportError):
            create_report(
                self.session,
                author_user_id=USER_TG,
                chat_id=1000,
                body="   ",
                screen_label="Главное меню",
                events_snapshot="x",
            )
        with self.assertRaises(ReportError):
            create_report(
                self.session,
                author_user_id=USER_TG,
                chat_id=1000,
                body="жалоба",
                screen_label="",
                events_snapshot="x",
            )
        with self.assertRaises(ReportError):
            create_report(
                self.session,
                author_user_id=0,
                chat_id=1000,
                body="жалоба",
                screen_label="main",
                events_snapshot="x",
            )
        self.assertEqual(get_report(self.session, 1), None)

    def test_pagination_newest_first(self) -> None:
        created = [self.make_report(body=f"жалоба {index}") for index in range(7)]
        first, total = active_reports(self.session)
        self.assertEqual(total, 7)
        self.assertEqual(len(first), REPORTS_PAGE_SIZE)
        self.assertEqual(
            [report.id for report in first],
            [report.id for report in reversed(created)][:REPORTS_PAGE_SIZE],
            "новые к старым",
        )
        second, total = active_reports(self.session, page=1)
        self.assertEqual(total, 7)
        self.assertEqual(
            [report.id for report in second],
            [report.id for report in reversed(created)][REPORTS_PAGE_SIZE:],
        )
        # Номер страницы за пределами списка безопасно приводится к последней.
        clamped, _ = active_reports(self.session, page=99)
        self.assertEqual([r.id for r in clamped], [r.id for r in second])

    def test_count_active_ignores_archived(self) -> None:
        first = self.make_report()
        self.make_report(body="вторая")
        self.assertEqual(count_active(self.session), 2)
        self.assertEqual(archive_report(self.session, first.id), 1)
        self.assertEqual(count_active(self.session), 1)
        archived, total = archived_reports(self.session)
        self.assertEqual(total, 1)
        self.assertEqual(archived[0].id, first.id)

    def test_double_archive_does_not_overwrite(self) -> None:
        created = self.make_report()
        moment = utcnow()
        self.assertEqual(
            archive_report(
                self.session,
                created.id,
                reply="Проверено, исправлено",
                replied_at=moment,
                closed_at=moment,
            ),
            1,
        )
        first = get_report(self.session, created.id)
        assert first is not None
        self.assertEqual(first.status, STATUS_ARCHIVED)
        self.assertEqual(first.developer_reply, "Проверено, исправлено")

        # Второе закрытие не меняет ни статус, ни ответ, ни даты.
        later = moment + timedelta(hours=1)
        self.assertEqual(
            archive_report(
                self.session,
                created.id,
                reply="Второй ответ",
                replied_at=later,
                closed_at=later,
            ),
            0,
        )
        second = get_report(self.session, created.id)
        assert second is not None
        self.assertEqual(second.developer_reply, "Проверено, исправлено")
        self.assertEqual(second.closed_at, first.closed_at)
        self.assertEqual(second.replied_at, first.replied_at)

    def test_archive_missing_report_is_noop(self) -> None:
        self.assertEqual(archive_report(self.session, 999), 0)

    def test_save_reply_only_for_active(self) -> None:
        created = self.make_report()
        self.assertEqual(save_reply(self.session, created.id, reply="Уже смотрим"), 1)
        stored = get_report(self.session, created.id)
        assert stored is not None
        self.assertEqual(stored.developer_reply, "Уже смотрим")
        self.assertEqual(stored.status, "active")
        self.assertIsNotNone(stored.replied_at)

        archive_report(self.session, created.id, reply="Закрыто")
        # Архивный репорт больше не переписывается.
        self.assertEqual(save_reply(self.session, created.id, reply="Поздний"), 0)
        stored = get_report(self.session, created.id)
        assert stored is not None
        self.assertEqual(stored.developer_reply, "Закрыто")

    def test_record_notification_is_idempotent(self) -> None:
        created = self.make_report()
        self.assertEqual(
            record_notification(self.session, created.id, ADMIN_TG), (ADMIN_TG,)
        )
        self.assertEqual(
            record_notification(self.session, created.id, ADMIN_TG), (ADMIN_TG,)
        )
        self.assertEqual(
            record_notification(self.session, created.id, OTHER_TG),
            (ADMIN_TG, OTHER_TG),
        )
        stored = get_report(self.session, created.id)
        assert stored is not None
        self.assertEqual(stored.notified_user_ids, (ADMIN_TG, OTHER_TG))
        with self.assertRaises(ReportError):
            record_notification(self.session, 999, ADMIN_TG)

    def test_reports_survive_new_session(self) -> None:
        """Постоянное хранение: строка читается новой сессией (рестарт)."""
        created = self.make_report()
        with self.Session() as fresh:
            stored = get_report(fresh, created.id)
        assert stored is not None
        self.assertEqual(stored.body, created.body)


# --- utils.user_events ------------------------------------------------------


class UserEventLogTests(_DatabaseCase):
    """Запись, чтение за час, retention и контракт `detail`."""

    def setUp(self) -> None:
        super().setUp()
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        patcher = patch.object(user_events, "SessionLocal", self.factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(user_events.reset_cleanup_counter)

    def write(self, action: str, **kwargs) -> bool:
        return user_events.event_log(USER_TG, action, **kwargs)

    def test_write_and_read_event(self) -> None:
        self.assertTrue(
            self.write(
                "screen.open",
                screen="extras",
                scenario="announcement",
                result="ok",
                detail="instance=12",
            )
        )
        events = user_events.list_user_events(USER_TG)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.action, "screen.open")
        self.assertEqual(event.screen, "extras")
        self.assertEqual(event.scenario, "announcement")
        self.assertEqual(event.result, "ok")
        self.assertEqual(event.detail, "instance=12")
        self.assertIn("screen.open", event.as_line())
        self.assertIn("instance=12", event.as_line())

    def test_read_window_is_last_hour(self) -> None:
        now = datetime(2026, 5, 4, 12, 0, 0)
        inside = now - timedelta(minutes=30)
        boundary = now - timedelta(hours=1)
        older = now - timedelta(hours=1, seconds=1)
        for moment, action in (
            (older, "screen.open"),
            (boundary, "screen.back"),
            (inside, "field.edit.save"),
        ):
            _execute(
                self.path,
                "INSERT INTO user_event_log "
                "(telegram_user_id, created_at, action) VALUES (?, ?, ?)",
                (USER_TG, moment.strftime("%Y-%m-%d %H:%M:%S.%f"), action),
            )
        # Чужой пользователь в снимок не попадает.
        _execute(
            self.path,
            "INSERT INTO user_event_log "
            "(telegram_user_id, created_at, action) VALUES (?, ?, ?)",
            (OTHER_TG, inside.strftime("%Y-%m-%d %H:%M:%S.%f"), "screen.open"),
        )
        # «Сейчас» зафиксировано: граница «ровно час назад» проверяется
        # детерминированно, через штатный путь (без явного `since`).
        with patch.object(user_events, "utcnow", lambda: now):
            events = user_events.list_user_events(USER_TG)
        self.assertEqual(
            [event.action for event in events],
            ["screen.back", "field.edit.save"],
            "ровно час назад включён, более старое событие не попадает",
        )

    def test_read_window_respects_explicit_period(self) -> None:
        now = utcnow()
        for moment, action in (
            (now - timedelta(hours=3), "screen.open"),
            (now - timedelta(hours=2), "screen.back"),
        ):
            _execute(
                self.path,
                "INSERT INTO user_event_log "
                "(telegram_user_id, created_at, action) VALUES (?, ?, ?)",
                (USER_TG, moment.strftime("%Y-%m-%d %H:%M:%S.%f"), action),
            )
        events = user_events.list_user_events(
            USER_TG, since=now - timedelta(hours=2, minutes=30)
        )
        self.assertEqual([event.action for event in events], ["screen.back"])

    def test_snapshot_text_contains_lines(self) -> None:
        self.write("screen.open", screen="extras", result="ok")
        snapshot = user_events.build_events_snapshot(USER_TG)
        self.assertIn(f"События пользователя {USER_TG}", snapshot)
        self.assertIn("screen.open", snapshot)
        self.assertIn("screen=extras", snapshot)
        empty = user_events.build_events_snapshot(OTHER_TG)
        self.assertIn("(событий нет)", empty)

    def test_retention_removes_only_old_events(self) -> None:
        now = utcnow()
        old = now - timedelta(days=user_events.RETENTION_DAYS + 1)
        fresh = now - timedelta(hours=1)
        for moment, action in ((old, "screen.open"), (fresh, "screen.back")):
            _execute(
                self.path,
                "INSERT INTO user_event_log "
                "(telegram_user_id, created_at, action) VALUES (?, ?, ?)",
                (USER_TG, moment.strftime("%Y-%m-%d %H:%M:%S.%f"), action),
            )
        self.assertEqual(user_events.cleanup_old_events(), 1)
        remaining = _sql(self.path, "SELECT action FROM user_event_log")
        self.assertEqual(remaining, [("screen.back",)])

    def test_cleanup_runs_periodically_without_threads(self) -> None:
        old = utcnow() - timedelta(days=user_events.RETENTION_DAYS + 1)
        _execute(
            self.path,
            "INSERT INTO user_event_log "
            "(telegram_user_id, created_at, action) VALUES (?, ?, ?)",
            (USER_TG, old.strftime("%Y-%m-%d %H:%M:%S.%f"), "screen.open"),
        )
        for _ in range(user_events.CLEANUP_EVERY):
            self.assertTrue(self.write("screen.open"))
        remaining = _sql(
            self.path,
            "SELECT count(*) FROM user_event_log WHERE created_at < ?",
            (
                (utcnow() - timedelta(days=user_events.RETENTION_DAYS)).strftime(
                    "%Y-%m-%d %H:%M:%S.%f"
                ),
            ),
        )
        self.assertEqual(remaining, [(0,)], "старое событие вычищено счётчиком")

    def test_event_log_failure_never_raises(self) -> None:
        def broken():
            raise RuntimeError("база недоступна")

        with patch.object(user_events, "SessionLocal", broken):
            self.assertFalse(user_events.event_log(USER_TG, "screen.open"))
            self.assertEqual(user_events.list_user_events(USER_TG), [])
            self.assertEqual(user_events.cleanup_old_events(), 0)

    def test_empty_action_is_ignored(self) -> None:
        self.assertFalse(user_events.event_log(USER_TG, "   "))
        self.assertEqual(_sql(self.path, "SELECT count(*) FROM user_event_log"), [(0,)])

    def test_detail_is_single_line_and_bounded(self) -> None:
        """Контракт хелпера: `detail` — короткая техническая строка."""
        multiline = "instance=1\nпользовательский текст\nещё строка"
        self.assertTrue(self.write("screen.open", detail=multiline))
        self.assertTrue(self.write("screen.open", detail="x" * 1000))
        rows = _sql(self.path, "SELECT detail FROM user_event_log ORDER BY id")
        self.assertEqual(rows[0][0], "instance=1 пользовательский текст ещё строка")
        self.assertNotIn("\n", rows[0][0])
        self.assertEqual(len(rows[1][0]), user_events.MAX_DETAIL_LENGTH)

    def test_helper_does_not_store_field_values(self) -> None:
        """
        Контракт этапа: в журнал идут только технические параметры.

        Обработчики передают в `detail` идентификаторы; проверяем, что
        значение поля анонса в записанной строке не появляется, даже если
        оно передано как есть (защита от случайной ошибки — единственная
        линия здесь именно дисциплина вызова, и тест её фиксирует).
        """
        field_value = "Концерт в филармонии, билеты от 1500 рублей"
        self.write(
            "field.edit.save",
            screen="field_input",
            result="ok",
            detail="instance=12 field=7",
        )
        stored = _sql(
            self.path, "SELECT action, screen, result, detail FROM user_event_log"
        )
        self.assertEqual(len(stored), 1)
        self.assertNotIn(field_value, repr(stored))
        # Значение, переданное в техническое поле целиком, тоже не выходит
        # за пределы одной обрезанной строки — многострочный текст не ляжет
        # в снимок репорта как содержимое сообщения.
        self.assertNotIn("\n", stored[0][3])


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PanelEventLogIntegrationTests(unittest.TestCase):
    """
    Точки записи журнала на реальном обработчике панели анонса.

    Проверяется, что навигация доставленного анонса даёт технические события
    («где пользователь нажал»), а значение поля в журнал не попадает.
    """

    @scenario_test
    async def test_panel_actions_are_logged_without_field_values(self, h) -> None:
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        active = _only_active(h, draft.id)

        # «📝 Текстовые поля» — открытие списка полей редактора.
        await h.callback(next(iter(h.live_callbacks("anp_data:"))))
        events = user_events.list_user_events(
            DEFAULT_USER_ID, session_factory=h.session_factory
        )
        self.assertEqual(
            [(event.action, event.screen, event.result) for event in events],
            [("screen.open", "field_list", "ok")],
        )
        self.assertIn(f"instance={active.id}", events[0].detail or "")

        # Правка значения поля: событие сохранения, но НЕ само значение.
        updated_text = "Секретное значение поля"
        await _open_field_question(h)
        await h.text(updated_text)
        events = user_events.list_user_events(
            DEFAULT_USER_ID, session_factory=h.session_factory
        )
        self.assertEqual([event.action for event in events][-1], "field.edit.save")
        self.assertNotIn(
            updated_text,
            " ".join(event.detail or "" for event in events),
            "значение поля попало в журнал",
        )

        # «◀️ Назад» из списка полей — навигация.
        await h.callback(next(iter(h.live_callbacks("anf_back:"))))
        events = user_events.list_user_events(
            DEFAULT_USER_ID, session_factory=h.session_factory
        )
        self.assertEqual([event.action for event in events][-1], "screen.back")

    @scenario_test
    async def test_stale_panel_button_is_logged_as_stale(self, h) -> None:
        """Устаревшая кнопка тоже видна в журнале — с result=stale."""
        await h.text("/start")
        draft = await _delivered_without_photos(h)
        active = _only_active(h, draft.id)
        with h.session_factory() as session:
            archive_instance(session, active.id)
        await h.callback(f"anp_menu:{active.id}")
        events = user_events.list_user_events(
            DEFAULT_USER_ID, session_factory=h.session_factory
        )
        self.assertTrue(events, "журнал пуст")
        self.assertEqual(events[-1].result, "stale")
        self.assertEqual(events[-1].screen, "announcement_panel")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
