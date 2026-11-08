# tests/test_poster2_24_p1_migrations.py
"""
Poster 2.4, этап P1 — независимая проверка миграции 25
(`single_address_brick`).

Контракт (Poster 2.4 §3.2):

- в модели остаётся ОДИН адресный кирпичик — системный `адрес` (id 10);
  легаси-определение `адрес_места` (id 6) устраняется полностью, без
  алиасов и переименования;
- на базе версии 24 миграция удаляет строку `bricks`, её привязки к версиям
  и снимкам шаблонов, значения черновиков и доп. кирпичики, конфликтные
  записи и ссылки предложений; FK/integrity остаются целыми;
- повторный запуск ничего не меняет (журнал и схема идемпотентны);
- чистая установка 0→25 и апгрейд 24→25 дают ОДИНАКОВУЮ схему
  (`sqlite_master` побайтово по каждому объекту);
- триггеры резервирования кодов и связки одобрения предложения пересозданы
  в редакции Poster 2.4 P1: повышение того же ряда (scope personal → system)
  разрешено, второй системный носитель кода — нет.

Тесты работают только с временными файлами SQLite; реальная `post_bot.db`
не открывается, `.env` не читается (токен подставляется в `tests/__init__`).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import unittest
from typing import cast

from migrations import DEFAULT_MIGRATIONS, LATEST_VERSION, run_migrations
from tests.stage19a_helpers import make_engine, temp_db_path

LEGACY_ADDRESS_CODE = "адрес_места"
ADDRESS_CODE = "адрес"
MIGRATION_25_NAME = "single_address_brick"


def schema_objects(path: str) -> dict[str, tuple[str, str, str | None]]:
    """Все объекты схемы: имя → (тип, таблица, SQL)."""
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT name, type, tbl_name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
    finally:
        connection.close()
    return {str(name): (str(kind), str(table), sql) for name, kind, table, sql in rows}


def triggers(path: str, table: str | None = None) -> dict[str, str]:
    connection = sqlite3.connect(path)
    try:
        statement = "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
        params: tuple[object, ...] = ()
        if table is not None:
            statement += " AND tbl_name = ?"
            params = (table,)
        rows = connection.execute(statement, params).fetchall()
    finally:
        connection.close()
    return {str(name): sql for name, sql in rows}


def count(path: str, sql: str, params: tuple[object, ...] = ()) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute(sql, params).fetchone()[0])
    finally:
        connection.close()


class _Migration25TestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.path = temp_db_path("poster-24-p1-migr-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)

    def migrate_to_24(self) -> None:
        engine = make_engine(self.path)
        try:
            self.assertEqual(run_migrations(engine, DEFAULT_MIGRATIONS[:24]), 24)
        finally:
            engine.dispose()

    def migrate_latest(self) -> int:
        engine = make_engine(self.path)
        try:
            return run_migrations(engine)
        finally:
            engine.dispose()

    def connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


class Migration25JournalTests(_Migration25TestCase):
    def test_version_25_follows_24_and_is_named(self) -> None:
        # Poster 2.5 C1 добавила 26 (`announcement_instances`), D1 — 27
        # (`template_conditional_parts`), 2.5.1 — 28
        # (`template_snapshot_conditional_parts`), этап B — 29
        # (`user_event_log_reports`); миграция 25 остаётся на своём месте.
        self.assertEqual(LATEST_VERSION, 30)
        self.assertEqual(
            [(migration.version, migration.name) for migration in DEFAULT_MIGRATIONS],
            [
                *[
                    (migration.version, migration.name)
                    for migration in DEFAULT_MIGRATIONS[:-6]
                ],
                (25, MIGRATION_25_NAME),
                (26, "announcement_instances"),
                (27, "template_conditional_parts"),
                (28, "template_snapshot_conditional_parts"),
                (29, "user_event_log_reports"),
                (30, "standard_template_extended"),
            ],
        )
        self.assertEqual(
            [migration.version for migration in DEFAULT_MIGRATIONS],
            list(range(1, 31)),
            "версии идут без пропусков и в порядке возрастания",
        )

    def test_upgrade_appends_version_25_after_24(self) -> None:
        self.migrate_to_24()
        self.assertEqual(self.migrate_latest(), LATEST_VERSION)
        connection = sqlite3.connect(self.path)
        try:
            versions = connection.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(
            versions[-7], (24, "template_content_and_extras_without_sections")
        )
        self.assertEqual(versions[-6], (25, MIGRATION_25_NAME))
        self.assertEqual(versions[-5], (26, "announcement_instances"))
        self.assertEqual(versions[-4], (27, "template_conditional_parts"))
        self.assertEqual(versions[-3], (28, "template_snapshot_conditional_parts"))
        self.assertEqual(versions[-2], (29, "user_event_log_reports"))
        self.assertEqual(versions[-1], (30, "standard_template_extended"))
        self.assertEqual(len(versions), 30)

    def test_repeated_run_is_idempotent(self) -> None:
        self.migrate_to_24()
        self.assertEqual(self.migrate_latest(), LATEST_VERSION)
        before = schema_objects(self.path)
        journal_before = count(self.path, "SELECT count(*) FROM schema_migrations")
        self.assertEqual(self.migrate_latest(), LATEST_VERSION)
        self.assertEqual(schema_objects(self.path), before)
        self.assertEqual(
            count(self.path, "SELECT count(*) FROM schema_migrations"), journal_before
        )


class Migration25FreshInstallTests(_Migration25TestCase):
    def test_fresh_install_has_single_address_brick(self) -> None:
        self.assertEqual(self.migrate_latest(), LATEST_VERSION)
        self.assertEqual(
            count(
                self.path,
                "SELECT count(*) FROM bricks WHERE code = ? AND scope = 'system'",
                (ADDRESS_CODE,),
            ),
            1,
            "ровно один системный кирпичик адреса",
        )
        self.assertEqual(
            count(
                self.path,
                "SELECT count(*) FROM bricks WHERE code = ?",
                (LEGACY_ADDRESS_CODE,),
            ),
            0,
            "легаси-определение «адрес_места» не создаётся",
        )
        # Номер 6 не переиспользуется: ID системных кирпичиков стабильны.
        self.assertEqual(
            count(self.path, "SELECT count(*) FROM bricks WHERE id = 6"),
            0,
        )
        system_ids = sorted(
            cast(int, row[0])
            for row in self._query("SELECT id FROM bricks WHERE scope='system'")
        )
        # Этап A1 (миграция 30): + `условие_входа`, `куратор`, `подвал`.
        self.assertEqual(
            system_ids, [1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14]
        )

    def _query(self, sql: str) -> list[tuple[object, ...]]:
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def test_fresh_and_upgraded_schemas_are_identical(self) -> None:
        fresh_path = temp_db_path("poster-24-p1-fresh-")
        self.addCleanup(shutil.rmtree, os.path.dirname(fresh_path), True)
        engine = make_engine(fresh_path)
        try:
            self.assertEqual(run_migrations(engine), LATEST_VERSION)
        finally:
            engine.dispose()

        self.migrate_to_24()
        self.assertEqual(self.migrate_latest(), LATEST_VERSION)

        fresh = schema_objects(fresh_path)
        upgraded = schema_objects(self.path)
        self.assertEqual(
            sorted(fresh),
            sorted(upgraded),
            "набор объектов схемы после апгрейда и чистой установки различается",
        )
        for name, definition in fresh.items():
            self.assertEqual(
                upgraded[name], definition, f"объект схемы {name!r} различается"
            )


class Migration25LegacyDataTests(_Migration25TestCase):
    """Наполненная база версии 24: миграция убирает только легаси-адрес."""

    def setUp(self) -> None:
        super().setUp()
        self.migrate_to_24()
        connection = self.connection()
        try:
            connection.execute(
                "INSERT INTO users (id, telegram_user_id, created_at) "
                "VALUES (1, 940001, '2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO places (id, created_by_user_id, created_at, city) "
                "VALUES (1, 1, '2026-01-01 00:00:00', 'Город')"
            )
            connection.execute(
                "INSERT INTO announcement_drafts "
                "(id, author_user_id, place_id, place_display_name, origin, "
                " stage, revision, created_at, updated_at) "
                "VALUES (1, 1, 1, 'Площадка', 'place', 'place', 1, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            # Значение легаси-адреса (id 6) и значение `[адрес]` (id 10).
            for value_id, brick_id, payload in (
                (1, 6, '{"text": "ул. Старая, 1"}'),
                (2, 10, '{"text": "ул. Новая, 2"}'),
            ):
                connection.execute(
                    "INSERT INTO announcement_values "
                    "(id, draft_id, brick_id, state, value_json, created_at, "
                    " updated_at) VALUES (?, 1, ?, 'valid', ?, "
                    "'2026-01-01 00:00:00', '2026-01-01 00:00:00')",
                    (value_id, brick_id, payload),
                )
            # Доп. кирпичик черновика того же легаси-адреса.
            connection.execute(
                "INSERT INTO announcement_extras "
                "(draft_id, brick_id, occurrence, created_at, updated_at) "
                "VALUES (1, 6, 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO templates "
                "(id, kind, owner_user_id, name, revision, created_by_user_id, "
                " created_at, updated_at) "
                "VALUES (1, 'personal', 1, 'Шаблон', 1, 1, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO template_versions "
                "(id, template_id, version, content, extended_content, "
                " created_by_user_id, created_at, builtin_version) "
                "VALUES (1, 1, 1, '[адрес_места] и [адрес]', NULL, 1, "
                "'2026-01-01 00:00:00', NULL)"
            )
            connection.execute(
                "INSERT INTO template_version_bricks "
                "(template_version_id, brick_id, code) VALUES (1, 6, 'адрес_места')"
            )
            connection.execute(
                "INSERT INTO template_version_bricks "
                "(template_version_id, brick_id, code) VALUES (1, 10, 'адрес')"
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(self.migrate_latest(), LATEST_VERSION)

    def test_legacy_rows_are_removed_and_neighbours_untouched(self) -> None:
        connection = sqlite3.connect(self.path)
        try:
            bricks = connection.execute(
                "SELECT id, code FROM bricks WHERE id IN (6, 10) ORDER BY id"
            ).fetchall()
            values = connection.execute(
                "SELECT brick_id FROM announcement_values ORDER BY id"
            ).fetchall()
            bindings = connection.execute(
                "SELECT brick_id, code FROM template_version_bricks ORDER BY brick_id"
            ).fetchall()
            extras = connection.execute(
                "SELECT count(*) FROM announcement_extras"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(bricks, [(10, ADDRESS_CODE)])
        self.assertEqual(values, [(10,)])
        self.assertEqual(bindings, [(10, ADDRESS_CODE)])
        self.assertEqual(extras, 0)
        # Текст версии шаблона не переписывается: миграция не правит контент.
        connection = sqlite3.connect(self.path)
        try:
            content = connection.execute(
                "SELECT content FROM template_versions WHERE id = 1"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(content, "[адрес_места] и [адрес]")

    def test_no_dangling_references_and_integrity_is_ok(self) -> None:
        connection = self.connection()
        try:
            for table, column in (
                ("announcement_values", "brick_id"),
                ("announcement_extras", "brick_id"),
                ("template_version_bricks", "brick_id"),
                ("template_snapshot_bricks", "brick_id"),
                ("brick_proposals", "source_brick_id"),
                ("brick_proposals", "approved_brick_id"),
                ("brick_conflicts", "system_brick_id"),
                ("brick_conflicts", "local_brick_id"),
            ):
                dangling = connection.execute(
                    f"SELECT count(*) FROM {table} AS t "
                    f"LEFT JOIN bricks AS b ON b.id = t.{column} "
                    f"WHERE t.{column} IS NOT NULL AND b.id IS NULL"
                ).fetchone()[0]
                self.assertEqual(dangling, 0, f"висячая ссылка в {table}.{column}")
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
        finally:
            connection.close()


class Migration25TriggerTests(_Migration25TestCase):
    """Триггеры защиты после миграции 25 — в редакции Poster 2.4 P1."""

    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(self.migrate_latest(), LATEST_VERSION)

    def test_system_brick_is_still_protected(self) -> None:
        connection = self.connection()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM bricks WHERE id = 10")
        finally:
            connection.close()
        self.assertEqual(
            count(self.path, "SELECT count(*) FROM bricks WHERE id = 10"), 1
        )

    def test_legacy_trigger_names_are_restored(self) -> None:
        names = triggers(self.path)
        self.assertEqual(
            names["trg_brick_proposals_approval_link"].split()[2],
            "trg_brick_proposals_approval_link",
        )
        for table in (
            "bricks",
            "template_version_bricks",
            "template_snapshot_bricks",
            "brick_conflicts",
            "brick_proposals",
        ):
            self.assertTrue(triggers(self.path, table), f"нет триггеров на {table}")

    def test_system_code_change_stays_immutable(self) -> None:
        connection = self.connection()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE bricks SET code='новый' WHERE id = 10")
        finally:
            connection.close()

    def test_promotion_of_same_row_is_allowed_but_second_row_is_not(self) -> None:
        """
        personal → system на том же ряду разрешено; INSERT второго
        системного носителя того же кода запрещён.
        """
        connection = self.connection()
        try:
            connection.execute(
                "INSERT INTO users (id, telegram_user_id, created_at) "
                "VALUES (1, 940025, '2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO bricks "
                "(id, code, title, type, scope, source, place_id, owner_user_id, "
                " rules_json, status, revision, created_by_user_id, created_at, "
                " updated_at) VALUES "
                "(100, 'промо_25', 'Промо', 'short_text', 'personal', 'ask', "
                " NULL, 1, NULL, 'active', 1, 1, '2026-01-01 00:00:00', NULL)"
            )
            connection.commit()
            connection.execute(
                "UPDATE bricks SET scope='system', owner_user_id=NULL, "
                "revision=revision+1 WHERE id = 100"
            )
            connection.commit()
            self.assertEqual(
                connection.execute(
                    "SELECT scope, owner_user_id FROM bricks WHERE id = 100"
                ).fetchone(),
                ("system", None),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO bricks "
                    "(code, title, type, scope, source, place_id, owner_user_id, "
                    " rules_json, status, revision, created_by_user_id, "
                    " created_at, updated_at) VALUES "
                    "('промо_25', 'Дубль', 'short_text', 'system', 'ask', NULL, "
                    " NULL, NULL, 'active', 1, NULL, '2026-01-01 00:00:00', NULL)"
                )
        finally:
            connection.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
