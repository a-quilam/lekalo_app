# tests/test_stage251_snapshot_rules_migration.py
"""
Poster 2.5.1 — миграция 28 и заморозка правил в снимке шаблона.

Контракт (spec §44, требование владельца релиза 2.5.1):

- миграция 28 добавляет ТОЛЬКО новую таблицу
  `template_snapshot_conditional_parts` и колонку
  `template_snapshots.conditional_rules_frozen`; backfill не выполняется;
- legacy-снимок (созданный до миграции) получает `frozen = 0` и продолжает
  рендериться по правилам версии черновика — прежний путь;
- `create_template_snapshot` в ОДНОЙ транзакции копирует активные правила
  версии и ставит `frozen = 1`; пустая заморозка (правил не было) — тоже
  `frozen = 1`, а не legacy;
- `stale`-правила версии в снимок не попадают: они и на версии не
  применялись;
- рендер снимка с `frozen = 1` читает только собственную копию правил:
  правка шаблона, перенос правила в новую версию и новое правило той же
  версии рендер снимка не меняют.

Тесты работают только с временными файлами SQLite; реальная `post_bot.db`,
`.env` и Telegram не используются.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import unittest

from sqlalchemy import select

from announcement_store import (
    ORIGIN_PERSONAL,
    build_announcement_preview,
    save_announcement_value,
    start_announcement_wizard,
)
from conditional_parts import (
    STATUS_STALE,
    create_conditional_part,
    list_conditional_parts,
    template_snapshot_conditional_parts,
)
from migrations import DEFAULT_MIGRATIONS, LATEST_VERSION, run_migrations
from models import Template, TemplateSnapshot
from template_store import create_personal_template, update_template_content
from tests.stage19a_helpers import make_engine, temp_db_path
from tests.stage23_helpers import OWNER_TG, TemplateDatabaseTestCase

MIGRATION_27_NAME = "template_conditional_parts"
MIGRATION_28_NAME = "template_snapshot_conditional_parts"

CONTENT_V1 = "**[main] – [cost]**\nКонец"
HIDDEN_FRAGMENT = " – [cost]"
EMPTY_COST_V1 = "<b>Концерт</b>\nКонец"


def _journal(path: str) -> list[tuple[int, str]]:
    connection = sqlite3.connect(path)
    try:
        return [
            (int(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            )
        ]
    finally:
        connection.close()


def _sql(path: str, statement: str, params: tuple = ()) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        return connection.execute(statement, params).fetchall()
    finally:
        connection.close()


def _columns(path: str, table: str) -> dict[str, tuple]:
    return {
        str(row[1]): (str(row[2]).upper(), int(row[3]), row[4])
        for row in _sql(path, f"PRAGMA table_info({table})")
    }


# --- миграция 28 ------------------------------------------------------------


class Migration28SnapshotRulesTests(unittest.TestCase):
    """Чистая установка, апгрейд с v27, ограничения и отсутствие backfill."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster-251-migr-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)

    def migrate(self, migrations=DEFAULT_MIGRATIONS) -> int:
        engine = make_engine(self.path)
        try:
            return run_migrations(engine, migrations)
        finally:
            engine.dispose()

    def test_fresh_install_has_snapshot_rules_table(self) -> None:
        self.assertEqual(self.migrate(), LATEST_VERSION)
        self.assertEqual(LATEST_VERSION, 28)
        self.assertEqual(
            _journal(self.path)[-2:],
            [
                (27, MIGRATION_27_NAME),
                (28, MIGRATION_28_NAME),
            ],
        )
        self.assertEqual(
            _columns(self.path, "template_snapshot_conditional_parts"),
            {
                "id": ("INTEGER", 1, None),
                "snapshot_id": ("INTEGER", 1, None),
                "section": ("TEXT", 1, None),
                "brick_id": ("INTEGER", 1, None),
                "fragment": ("TEXT", 1, None),
                "occurrence": ("INTEGER", 1, "1"),
                "created_at": ("DATETIME", 1, None),
            },
        )
        self.assertEqual(
            _sql(
                self.path,
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='template_snapshot_conditional_parts' "
                "AND name NOT LIKE 'sqlite_%'",
            ),
            [("ix_template_snapshot_conditional_parts_snapshot_section",)],
        )
        snapshot_columns = _columns(self.path, "template_snapshots")
        self.assertIn("conditional_rules_frozen", snapshot_columns)
        self.assertEqual(
            snapshot_columns["conditional_rules_frozen"],
            ("INTEGER", 1, "0"),
            "признак обязателен и по умолчанию 0 (legacy)",
        )
        self.assertEqual(_sql(self.path, "PRAGMA foreign_key_check"), [], "FK целы")
        self.assertEqual(
            _sql(self.path, "PRAGMA integrity_check"), [("ok",)], "целостность"
        )

    def test_upgrade_from_v27_adds_only_new_objects(self) -> None:
        """Upgrade-путь: база v27 получает ровно таблицу, индекс и колонку."""
        self.assertEqual(self.migrate(DEFAULT_MIGRATIONS[:27]), 27)
        before = {
            str(row[0]) for row in _sql(self.path, "SELECT name FROM sqlite_master")
        }
        self.assertNotIn("template_snapshot_conditional_parts", before)
        self.assertNotIn(
            "conditional_rules_frozen", _columns(self.path, "template_snapshots")
        )
        self.assertEqual(self.migrate(), LATEST_VERSION)
        after = {
            str(row[0]) for row in _sql(self.path, "SELECT name FROM sqlite_master")
        }
        self.assertEqual(
            after - before,
            {
                "template_snapshot_conditional_parts",
                "ix_template_snapshot_conditional_parts_snapshot_section",
            },
            "миграция только добавочная",
        )
        self.assertEqual(_journal(self.path)[-1], (28, MIGRATION_28_NAME))
        self.assertEqual(self.migrate(), LATEST_VERSION)
        self.assertEqual(
            {str(row[0]) for row in _sql(self.path, "SELECT name FROM sqlite_master")},
            after,
            "повторный запуск ничего не меняет",
        )

    def test_legacy_snapshot_is_not_backfilled(self) -> None:
        """Backfill не делается: существующий снимок остаётся legacy (0)."""
        engine = make_engine(self.path)
        try:
            run_migrations(engine, DEFAULT_MIGRATIONS[:27])
        finally:
            engine.dispose()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO users (id, telegram_user_id, created_at) "
                "VALUES (1, 9928001, '2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO templates (id, kind, owner_user_id, revision, "
                "created_at, updated_at) VALUES ("
                "1, 'personal', 1, 1, '2026-01-01 00:00:00', "
                "'2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO template_versions (id, template_id, version, content, "
                "created_at) VALUES (1, 1, 1, 'Цена: [цена]', "
                "'2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO template_snapshots (id, owner_user_id, "
                "source_template_id, source_template_version, source_kind, "
                "content, created_at) VALUES (1, 1, 1, 1, 'personal', "
                "'Цена: [цена]', '2026-01-01 00:00:00')"
            )
            connection.commit()
        finally:
            connection.close()

        self.assertEqual(self.migrate(), LATEST_VERSION)
        self.assertEqual(
            _sql(self.path, "SELECT conditional_rules_frozen FROM template_snapshots"),
            [(0,)],
            "старый снимок остаётся legacy: правила не заморожены",
        )
        self.assertEqual(
            _sql(self.path, "SELECT count(*) FROM template_snapshot_conditional_parts"),
            [(0,)],
            "строки правил не переносятся задним числом",
        )

    def test_table_enforces_section_occurrence_and_foreign_keys(self) -> None:
        """CHECK-и схемы и FK: секция, вхождение, существование снимка."""
        self.assertEqual(self.migrate(), LATEST_VERSION)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO users (id, telegram_user_id, created_at) "
                "VALUES (1, 9928002, '2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO bricks (id, code, title, type, scope, source, status, "
                "revision, owner_user_id, created_at) VALUES ("
                "900, 'цена', 'Цена', 'short_text', 'personal', 'ask', 'active', "
                "1, 1, '2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO templates (id, kind, owner_user_id, revision, "
                "created_at, updated_at) VALUES ("
                "1, 'personal', 1, 1, '2026-01-01 00:00:00', "
                "'2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO template_versions (id, template_id, version, content, "
                "created_at) VALUES (1, 1, 1, 'Цена: [цена]', "
                "'2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO template_snapshots (id, owner_user_id, "
                "source_template_id, source_template_version, source_kind, "
                "content, created_at) VALUES (1, 1, 1, 1, 'personal', "
                "'Цена: [цена]', '2026-01-01 00:00:00')"
            )

            def insert(**overrides) -> None:
                values = {
                    "id": "1",
                    "snapshot_id": "1",
                    "section": "'content'",
                    "brick_id": "900",
                    "fragment": "'Цена: [цена]'",
                    "occurrence": "1",
                    "created_at": "'2026-01-01 00:00:00'",
                }
                values.update(overrides)
                connection.execute(
                    "INSERT INTO template_snapshot_conditional_parts ("
                    + ", ".join(values)
                    + ") VALUES ("
                    + ", ".join(values.values())
                    + ")"
                )

            insert()
            for bad in (
                {"id": "2", "section": "'header'"},
                {"id": "3", "occurrence": "0"},
                {"id": "4", "snapshot_id": "999"},
                {"id": "5", "brick_id": "999"},
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    insert(**bad)
        finally:
            connection.close()


# --- заморозка при создании снимка и рендер ---------------------------------


class SnapshotRulesFreezeTests(TemplateDatabaseTestCase):
    """Снимок копирует активные правила версии и дальше живёт своей жизнью."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_user(OWNER_TG, username="snapshot-rules-owner")
        self.main_brick = self.make_personal_brick(self.owner, "main")
        self.cost_brick = self.make_personal_brick(self.owner, "cost")
        self.template = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="С ценой",
            content=CONTENT_V1,
        )

    def _add_rule(self, *, fragment: str = HIDDEN_FRAGMENT, section: str = "content"):
        return create_conditional_part(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.template.template_id,
            expected_revision=self._revision(),
            section=section,
            brick_id=self.cost_brick,
            fragment=fragment,
        )

    def _revision(self) -> int:
        return int(
            self.session.execute(
                select(Template.revision).where(
                    Template.id == self.template.template_id
                )
            ).scalar_one()
        )

    def _start_draft(self):
        return start_announcement_wizard(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            origin=ORIGIN_PERSONAL,
            template_id=self.template.template_id,
        )

    def _save(self, draft, brick_id: int, value: object):
        return save_announcement_value(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=brick_id,
            value=value,
            expected_revision=draft.revision,
        )

    def _preview(self, draft) -> str:
        return build_announcement_preview(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
        ).html

    def _frozen_rows(self, snapshot_id: int) -> list[tuple]:
        return [
            (
                str(row.section),
                int(row.brick_id),
                str(row.fragment),
                int(row.occurrence),
            )
            for row in self.session.execute(
                select(
                    template_snapshot_conditional_parts.c.section,
                    template_snapshot_conditional_parts.c.brick_id,
                    template_snapshot_conditional_parts.c.fragment,
                    template_snapshot_conditional_parts.c.occurrence,
                )
                .where(template_snapshot_conditional_parts.c.snapshot_id == snapshot_id)
                .order_by(template_snapshot_conditional_parts.c.id)
            ).all()
        ]

    def test_snapshot_freezes_active_rules_and_flags_them(self) -> None:
        """Активное правило версии копируется, признак заморозки = 1."""
        created = self._add_rule()
        self.assertEqual(created.status, "created", created.message)

        draft = self._start_draft()
        snapshot = draft.template_snapshot
        assert snapshot is not None
        snapshot_id = snapshot.snapshot_id
        row = self.session.get(TemplateSnapshot, snapshot_id)
        assert row is not None
        self.assertEqual(row.conditional_rules_frozen, 1, "правила заморожены")
        self.assertEqual(
            self._frozen_rows(snapshot_id),
            [("content", self.cost_brick, HIDDEN_FRAGMENT, 1)],
            "копия совпадает с активным правилом версии",
        )

        draft = self._save(draft, self.main_brick, "Концерт")
        self.assertEqual(self._preview(draft), EMPTY_COST_V1)

    def test_snapshot_without_rules_is_frozen_but_empty(self) -> None:
        """Нет активных правил — всё равно frozen = 1, строк правил нет."""
        draft = self._start_draft()
        snapshot = draft.template_snapshot
        assert snapshot is not None
        snapshot_id = snapshot.snapshot_id
        row = self.session.get(TemplateSnapshot, snapshot_id)
        assert row is not None
        self.assertEqual(
            row.conditional_rules_frozen,
            1,
            "пустая заморозка — семантика «на момент создания правил не было»",
        )
        self.assertEqual(self._frozen_rows(snapshot_id), [])
        draft = self._save(draft, self.main_brick, "Концерт")
        self.assertEqual(
            self._preview(draft),
            "<b>Концерт – </b>\nКонец",
            "без правила фрагмент не скрывается",
        )

    def test_stale_rules_are_not_frozen(self) -> None:
        """`stale`-правило не применялось на версии и в снимок не попадает."""
        # 2.5.3 §3: условия живут только в расширенном тексте, а `stale`
        # возникает лишь при неоднозначном фрагменте (исчезнувший кирпичик
        # теперь удаляет условие).
        updated = update_template_content(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.template.template_id,
            expected_revision=self._revision(),
            content=CONTENT_V1,
            extended_content=CONTENT_V1,
        )
        self.assertTrue(updated.ok, updated.message)
        self.assertEqual(self._add_rule(section="extended_content").status, "created")
        updated = update_template_content(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.template.template_id,
            expected_revision=self._revision(),
            content=CONTENT_V1,
            extended_content=CONTENT_V1 + "\nповтор: – [cost]",
        )
        self.assertTrue(updated.ok, updated.message)
        parts = list_conditional_parts(
            self.session, OWNER_TG, self.template.template_id, include_stale=True
        )
        self.assertEqual([part.status for part in parts], [STATUS_STALE])

        draft = self._start_draft()
        snapshot = draft.template_snapshot
        assert snapshot is not None
        snapshot_id = snapshot.snapshot_id
        self.assertEqual(self._frozen_rows(snapshot_id), [])

    def test_snapshot_render_ignores_new_rule_of_same_version(self) -> None:
        """Новое правило той же версии к уже созданному снимку не применяется."""
        draft = self._start_draft()
        draft = self._save(draft, self.main_brick, "Концерт")
        before = self._preview(draft)
        self.assertEqual(before, "<b>Концерт – </b>\nКонец")

        self.assertEqual(self._add_rule().status, "created")
        self.session.expire_all()
        self.assertEqual(
            self._preview(draft),
            before,
            "правила снимка заморожены: новое правило версии не влияет",
        )

    def test_snapshot_rows_survive_template_edit(self) -> None:
        """Правка шаблона меняет правило версии, но не замороженную копию."""
        self.assertEqual(self._add_rule().status, "created")
        draft = self._start_draft()
        snapshot = draft.template_snapshot
        assert snapshot is not None
        snapshot_id = snapshot.snapshot_id
        draft = self._save(draft, self.main_brick, "Концерт")
        before = self._preview(draft)
        frozen_before = self._frozen_rows(snapshot_id)

        updated = update_template_content(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.template.template_id,
            expected_revision=self._revision(),
            content=CONTENT_V1 + "\nНовая строка",
        )
        self.assertTrue(updated.ok, updated.message)
        self.session.expire_all()

        self.assertEqual(
            self._frozen_rows(snapshot_id), frozen_before, "копия не переписана"
        )
        self.assertEqual(self._preview(draft), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
