# tests/test_db_maintenance_rebuild.py
"""
Rebuild-очистка тестовой БД (утверждённый контракт владельца).

Проверяется на ВРЕМЕННОЙ базе (рабочая Poster/post_bot.db не открывается):
после наполнения полноценными тестовыми данными через публичные сервисы
(площадка, пользователь, администраторы, локальные и личные кирпичики,
пользовательские шаблоны, предложение, черновик и готовый анонс) механизм
обслуживания обязан оставить ровно системные объекты:

- журнал миграций: полный, 1..актуальная версия кода (не зашита);
- системные кирпичики: коды совпадают с каталогом brick_types (не зашит);
- ВСЕ несистемные счётчики — нулевые (контракт: ни одной тестовой записи);
- встроенный стандартный шаблон — эталон кода (BUILTIN_TEMPLATE), строка
  материализуется вместе с первым местом; в чистой базе строк шаблонов нет;
- конфигурация/суперадмин — вне БД (.env), механизм их не читает.

Бэкап обязателен, прежняя база сохраняется (`.replaced-`), замена атомарна.
Запуск бота запрещён правилами; PID-файл службы в тестах не существует.
"""

from __future__ import annotations

import os
import sqlite3
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import db_maintenance
import tests  # noqa: F401 — фикстурный токен до импорта config (discover -s tests)
from announcement_store import (
    add_announcement_extra,
    begin_announcement_publication,
    choose_announcement_template,
    confirm_announcement_place,
    mark_announcement_publication_sent,
    save_announcement_value,
    set_announcement_stage,
)
from announcement_wizard import WizardStage
from brick_proposal_store import create_brick_proposal
from brick_store import create_place_brick
from brick_types import SYSTEM_BRICK_CODES
from migrations import DEFAULT_MIGRATIONS
from models import PlaceAdmin
from place_invite_store import ensure_invite
from place_store import add_alternative_name
from template_store import get_standard_template
from tests.stage19a_helpers import connect, make_engine, temp_db_path
from tests.stage23_helpers import OWNER_TG
from tests.stage27_helpers import AnnouncementDatabaseTestCase

TODAY = date(2026, 5, 1)


def table_counts(path: "str | Path") -> dict[str, int]:
    """Счётчики всех таблиц временной базы (кроме журнала миграций)."""
    connection = connect(str(path))
    try:
        names = [
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            if str(row["name"]) not in db_maintenance.SYSTEM_TABLES
        ]
        return {
            name: int(
                connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            )  # noqa: S608
            for name in sorted(names)
        }
    finally:
        connection.close()


class RebuildCleanupTestCase(AnnouncementDatabaseTestCase):
    """
    Временная база с полным набором тестовых данных (сценарий задания 7).

    AnnouncementDatabaseTestCase (Stage 27) уже создаёт владельца, место
    с адресом/ссылкой, альтернативное название, админа и стандарт; здесь
    добавляются остальные сущности контракта очистки. Суперадмин —
    конфигурационный (patch config.SUPERADMIN_USER_IDS), как в продакшене:
    строк в БД у него нет, права сохраняются вместе с конфигурацией,
    которую механизм не читает.
    """

    def setUp(self) -> None:
        super().setUp()
        # Таблицы моделей вне миграций (user_states, *_sessions) создаёт
        # create_all — тот же шаг штатного init_db; helpers Stage 23/27
        # применяют только миграции.
        from base import Base

        Base.metadata.create_all(bind=self.engine)
        # Базовый класс Stage 27 уже создал владельца-админа места; у места
        # может быть только ОДИН действующий администратор
        # (ux_place_admins_one_active), поэтому «администраторы» контракта —
        # владелец-админ плюс отозванное назначение другого пользователя.
        from models import utcnow

        revoked = PlaceAdmin(
            place_id=self.place.id,
            user_id=self.other.id,
            assigned_by_user_id=self.owner.id,
            assigned_at=utcnow(),
            revoked_by_user_id=self.owner.id,
            revoked_at=utcnow(),
        )
        self.session.add(revoked)
        self.session.commit()
        add_alternative_name(
            self.session,
            self.place.id,
            "ДК «Rebuild»",
            created_by_user_id=self.owner.id,
        )
        ensure_invite(
            self.session, actor_telegram_user_id=OWNER_TG, place_id=self.place.id
        )
        self.standard = get_standard_template(self.session, OWNER_TG, self.place.id)

        # Локальный (place) и личные кирпичики; «rebuild_add» нужен только
        # в extended_content личного шаблона — доп. поле анонса (E1).
        self.place_brick_id = create_place_brick(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            place_id=self.place.id,
            code="rebuild_place",
            title="Локальный rebuild",
            value_type="short_text",
        ).id
        self.personal_brick_id = self.make_personal_brick(self.owner, "rebuild_own")
        self.extra_brick_id = self.make_personal_brick(self.owner, "rebuild_add")

        # Пользовательские шаблоны: личный и правленый стандарт места.
        self.personal_template = self.make_personal_template(
            self.owner,
            "Личный rebuild",
            content="Анонс [rebuild_own]",
            extended_content="Доп [rebuild_add]",
        )
        from template_store import update_template_content

        update_template_content(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.standard.template_id,
            expected_revision=self.standard.revision,
            content="Стандарт [место] [rebuild_place]",
        )
        self.session.expire_all()

    def populate_announcements(self) -> None:
        """Черновик и готовый (sent) анонс — доводятся сервисом, не INSERT."""
        for publish in (False, True):
            draft = self.start_draft()
            confirmed = confirm_announcement_place(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                expected_revision=draft.revision,
            )
            chosen = choose_announcement_template(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=confirmed.draft_id,
                template_id=self.personal_template.template_id,
                expected_revision=confirmed.revision,
            )
            saved = save_announcement_value(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=chosen.draft_id,
                brick_id=self.personal_brick_id,
                value="Значение rebuild",
                expected_revision=chosen.revision,
                today=TODAY,
            )
            with_extras = add_announcement_extra(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=saved.draft_id,
                brick_id=self.extra_brick_id,
                value="Доп. значение rebuild",
                expected_revision=saved.revision,
            )
            at_preview = set_announcement_stage(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=saved.draft_id,
                stage=WizardStage.PREVIEW,
                expected_revision=with_extras.revision,
            )
            if not publish:
                continue
            publication = begin_announcement_publication(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=at_preview.draft_id,
                expected_revision=at_preview.revision,
                today=TODAY,
            )
            mark_announcement_publication_sent(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=publication.draft.draft_id,
                message_ids=[777001],
                fingerprint="rebuild-fingerprint",
            )

    def make_proposal(self) -> None:
        """Предложение повышения личного кирпичика (журнал BrickProposal)."""
        create_brick_proposal(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            source_brick_id=self.personal_brick_id,
            scope="place",
            place_id=self.place.id,
        )

    def seed_interface_states(self) -> None:
        """Интерфейсные состояния (place/brick/template/admin) и WIP-пост."""
        from models import (
            AdminSession,
            BrickSession,
            PlaceSession,
            TemplateSession,
            UserState,
            utcnow,
        )

        payload = '{"phase": "rebuild"}'
        session = self.session
        session.add(
            AdminSession(
                telegram_user_id=OWNER_TG, payload=payload, updated_at=utcnow()
            )
        )
        session.add(
            PlaceSession(
                telegram_user_id=OWNER_TG, payload=payload, updated_at=utcnow()
            )
        )
        session.add(
            BrickSession(
                telegram_user_id=OWNER_TG, payload=payload, updated_at=utcnow()
            )
        )
        session.add(
            TemplateSession(
                telegram_user_id=OWNER_TG, payload=payload, updated_at=utcnow()
            )
        )
        session.add(
            UserState(
                user_id=self.owner.id,
                chat_id=OWNER_TG,
                screen_message_id=42,
                payload='{"current_step": 1}',
                updated_at=utcnow(),
            )
        )
        session.commit()


class RebuildCleanupTests(RebuildCleanupTestCase):
    """Контракт очистки: до — данные, после — нули и системные объекты."""

    def setUp(self) -> None:
        super().setUp()
        self.populate_announcements()
        self.make_proposal()
        self.seed_interface_states()
        self.before = table_counts(self.path)

        # Проверяем, что наполнение состоялось: без этого тест доказывал бы
        # очистку пустой базы.
        self.assertGreater(self.before["users"], 0)
        self.assertGreater(self.before["places"], 0)
        self.assertGreater(self.before["templates"], 0)
        self.assertGreater(self.before["template_versions"], 0)
        self.assertGreater(self.before["template_snapshots"], 0)
        self.assertGreater(self.before["template_version_bricks"], 0)
        self.assertGreater(self.before["template_snapshot_bricks"], 0)
        self.assertGreater(self.before["bricks"], len(SYSTEM_BRICK_CODES))
        self.assertGreater(self.before["place_admins"], 0)
        self.assertGreater(self.before["place_names"], 0)
        self.assertGreater(self.before["place_invite_links"], 0)
        self.assertGreater(self.before["brick_proposals"], 0)
        self.assertGreater(self.before["announcement_drafts"], 0)
        self.assertGreater(self.before["announcement_values"], 0)
        self.assertGreater(self.before["announcement_extras"], 0)
        self.assertGreater(self.before["user_states"], 0)
        self.assertGreater(self.before["admin_sessions"], 0)
        self.assertGreater(self.before["place_sessions"], 0)
        self.assertGreater(self.before["brick_sessions"], 0)
        self.assertGreater(self.before["template_sessions"], 0)

        # PID-файл службы в тестах не существует (бот не запущен).
        assert not db_maintenance.SERVICE_PID_FILE.exists()

        # Механизм требует предварительный проверенный бэкап этой базы —
        # тот же контракт, что и в продакшене.
        db_maintenance._VERIFIED_BACKUPS.add(str(Path(self.path).resolve()))
        self.report = db_maintenance.run_maintenance(self.path, clean=True)
        self.after = table_counts(self.path)

    def test_all_user_counters_are_zero(self) -> None:
        """Контракт: ни одной несистемной записи после очистки."""
        for table, count in self.after.items():
            if table == "bricks":
                # bricks — исключение по контракту: ровно системный каталог.
                self.assertEqual(count, len(SYSTEM_BRICK_CODES))
            else:
                self.assertEqual(count, 0, f"{table} содержит тестовые данные")

    def test_migration_journal_is_complete(self) -> None:
        """Схема новой базы: полный журнал 1..актуальная версия кода."""
        connection = connect(self.path)
        try:
            versions = [
                int(row[0])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
        finally:
            connection.close()
        self.assertEqual(versions, list(range(1, len(DEFAULT_MIGRATIONS) + 1)))

    def test_system_brick_catalog_matches_code(self) -> None:
        """Системные кирпичики: ровно каталог brick_types (не зашит)."""
        connection = connect(self.path)
        try:
            codes = [
                str(row[0])
                for row in connection.execute(
                    "SELECT code FROM bricks WHERE scope='system' "
                    "AND status='active' ORDER BY id"
                )
            ]
        finally:
            connection.close()
        self.assertEqual(tuple(codes), SYSTEM_BRICK_CODES)

    def test_replaced_database_preserved(self) -> None:
        """Восстановление при неудаче: прежняя база сохранена рядом."""
        # Точный шаблон: только сама база `<имя>.replaced-<метка>`.
        # WAL-сателлиты (`<имя>-wal.replaced-…`, `<имя>-shm.replaced-…`)
        # тоже несут суффикс, но порядок scandir случаен — под них не годится.
        replaced = list(
            Path(self.path).parent.glob(
                Path(self.path).name + db_maintenance._REPLACED_SUFFIX + "*"
            )
        )
        self.assertTrue(replaced, "файл .replaced- не найден")
        # Прежняя база содержит те же данные наполнения — это она и есть.
        self.assertEqual(table_counts(replaced[0]), self.before)

    def test_backup_created(self) -> None:
        """WAL-aware бэкап фазы 2 создан и зафиксирован в отчёте."""
        backup = self.report.get("backup")
        self.assertIsInstance(backup, str)
        self.assertTrue(Path(str(backup)).exists())

    def test_counters_before_were_full(self) -> None:
        """Отчёт фазы 2 показал непустые счётчики ДО очистки (задание 1)."""
        counts_before = self.report.get("user_counts_before")
        assert isinstance(counts_before, dict)
        self.assertGreater(counts_before["users"], 0)
        self.assertGreater(counts_before["places"], 0)


class RebuildGuardTests(unittest.TestCase):
    """Запреты механизма: без бэкапа и при работающей службе очистки нет."""

    def test_clean_requires_verified_backup(self) -> None:
        path = temp_db_path("poster-rebuild-nobackup-")
        engine = make_engine(path)
        from migrations import run_migrations

        run_migrations(engine)
        engine.dispose()
        with self.assertRaises(db_maintenance.MaintenanceError):
            db_maintenance.clean_test_data(path)
        # База не изменена.
        connection = sqlite3.connect(path)
        try:
            users = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(users, 0)

    def test_clean_refuses_running_service(self) -> None:
        """Живой PID службы запрещает rebuild (замена файла под ботом)."""
        path = temp_db_path("poster-rebuild-live-")
        engine = make_engine(path)
        from migrations import run_migrations

        run_migrations(engine)
        engine.dispose()
        db_path = Path(path).resolve()
        db_maintenance._VERIFIED_BACKUPS.add(str(db_path))
        fake_pid_file = Path(path).parent / "post_bot.pid"
        fake_pid_file.write_text(str(1_000_000_000), encoding="utf-8")
        try:
            with (
                patch.object(db_maintenance, "SERVICE_PID_FILE", fake_pid_file),
                patch.object(os, "kill", lambda *_: None),
            ):
                with self.assertRaises(db_maintenance.MaintenanceError):
                    db_maintenance.clean_test_data(path)
        finally:
            db_maintenance._VERIFIED_BACKUPS.discard(str(db_path))
            fake_pid_file.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
