# tests/test_stage26_b1_catalogs_store.py
"""
«Лекало» 2.6, этап B1: слой данных справочников кураторов и подвалов.

Проверяется без UI и Telegram-обработчиков:

- миграция 31 на чистой базе (1..31), повторный прогон без дублей, XOR-область
  (`place_id` либо `owner_user_id`), индексы и уникальность названий подвалов;
- `catalogs_store`: создание/чтение/редактирование с сохранением
  форматирования (entities → безопасный HTML, переносы строк), архивирование
  и восстановление без физического удаления, пагинация;
- подвал: название для списка, текст — для подстановки;
- права: справочник площадки — только её администратор (и суперадмин),
  личный — только владелец; изоляция площадок и пользователей.

Тесты работают только с временными файлами SQLite в /tmp; реальная
`post_bot.db`, `.env` и Telegram не используются.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import unittest
from typing import Any
from unittest.mock import patch

from sqlalchemy.orm import Session, sessionmaker
from telegram import MessageEntity

import catalogs_store as store
from migrations import DEFAULT_MIGRATIONS, LATEST_VERSION, run_migrations
from models import Place, PlaceAdmin, User, utcnow
from place_store import create_place
from tests.stage19a_helpers import make_engine, table_names, temp_db_path
from user_store import get_or_create_user
from utils.pagination import PAGE_SIZE
from utils.tg_html import capture_text

MIGRATION_31_NAME = "curator_footer_catalogs"

OWNER_TG = 9_940_001
OTHER_TG = 9_940_002
ADMIN_TG = 9_940_003
STRANGER_TG = 9_940_004
SUPERADMIN_TG = 9_940_005


def _sql(path: str, statement: str, params: tuple = ()) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        return connection.execute(statement, params).fetchall()
    finally:
        connection.close()


def _journal(path: str) -> list[tuple[int, str]]:
    return [
        (int(row[0]), str(row[1]))
        for row in _sql(
            path, "SELECT version, name FROM schema_migrations ORDER BY version"
        )
    ]


def _indexes(path: str, table: str) -> set[str]:
    return {
        str(row[1])
        for row in _sql(path, f"PRAGMA index_list({table})")
        if not str(row[1]).startswith("sqlite_")
    }


# --- миграция 31 ------------------------------------------------------------


class Migration31Tests(unittest.TestCase):
    """Чистая база, повторный прогон и ограничения схемы."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster-26-b1-migr-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)

    def test_latest_version_is_thirty_three(self) -> None:
        self.assertEqual(LATEST_VERSION, 33)
        self.assertEqual(
            (DEFAULT_MIGRATIONS[-2].version, DEFAULT_MIGRATIONS[-2].name),
            (32, "system_brick_hints"),
        )
        self.assertEqual(
            (DEFAULT_MIGRATIONS[-1].version, DEFAULT_MIGRATIONS[-1].name),
            (33, "contact_brick_hint"),
        )
        self.assertEqual([m.version for m in DEFAULT_MIGRATIONS], list(range(1, 34)))

    def test_fresh_database_creates_both_tables_and_indexes(self) -> None:
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.assertEqual(_journal(self.path)[-2], (32, "system_brick_hints"))
        tables = table_names(self.path)
        self.assertIn("curator_entries", tables)
        self.assertIn("footer_entries", tables)
        self.assertTrue(
            {
                "ix_curator_entries_place_status",
                "ix_curator_entries_owner_status",
                "ux_footer_entries_place_name",
                "ux_footer_entries_owner_name",
                "ix_footer_entries_place_status",
                "ix_footer_entries_owner_status",
            }
            <= _indexes(self.path, "curator_entries")
            | _indexes(self.path, "footer_entries")
        )
        self.assertEqual(_sql(self.path, "PRAGMA foreign_key_check"), [])
        self.assertEqual(_sql(self.path, "PRAGMA integrity_check"), [("ok",)])

    def test_upgrade_from_v30_adds_only_catalog_objects(self) -> None:
        self.assertEqual(run_migrations(self.engine, DEFAULT_MIGRATIONS[:30]), 30)
        before = {
            str(row[0]) for row in _sql(self.path, "SELECT name FROM sqlite_master")
        }
        self.assertNotIn("curator_entries", before)
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        after = {
            str(row[0]) for row in _sql(self.path, "SELECT name FROM sqlite_master")
        }
        self.assertEqual(
            after - before,
            {
                "curator_entries",
                "ix_curator_entries_place_status",
                "ix_curator_entries_owner_status",
                "trg_curator_entries_id_immutable",
                "footer_entries",
                "ux_footer_entries_place_name",
                "ux_footer_entries_owner_name",
                "ix_footer_entries_place_status",
                "ix_footer_entries_owner_status",
                "trg_footer_entries_id_immutable",
            },
            "миграция 31 только добавочная",
        )
        self.assertEqual(_journal(self.path)[-2], (32, "system_brick_hints"))
        self.assertEqual(_sql(self.path, "PRAGMA foreign_key_check"), [])

    def test_migrated_schema_equals_fresh_create_all(self) -> None:
        from base import Base

        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        fresh_path = temp_db_path("poster-26-b1-fresh-")
        self.addCleanup(shutil.rmtree, os.path.dirname(fresh_path), True)
        fresh_engine = make_engine(fresh_path)
        self.addCleanup(fresh_engine.dispose)
        Base.metadata.create_all(fresh_engine)

        def schema(path: str, table: str) -> tuple:
            connection = sqlite3.connect(path)
            try:
                columns = [
                    (str(row[1]), str(row[2]).upper(), int(row[3]), row[4], row[5])
                    for row in connection.execute(f"PRAGMA table_info({table})")
                ]
                indexes = sorted(
                    (str(row[1]), int(row[2]), str(row[3]))
                    for row in connection.execute(f"PRAGMA index_list({table})")
                )
                foreign_keys = sorted(
                    tuple(str(value) for value in row[2:7])
                    for row in connection.execute(f"PRAGMA foreign_key_list({table})")
                )
                return (columns, indexes, foreign_keys)
            finally:
                connection.close()

        for table in ("curator_entries", "footer_entries"):
            with self.subTest(table=table):
                self.assertEqual(
                    schema(self.path, table),
                    schema(fresh_path, table),
                    "колонки, индексы и FK после миграции = модели",
                )

    def test_repeat_run_is_idempotent(self) -> None:
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        before = _sql(self.path, "SELECT name, sql FROM sqlite_master ORDER BY name")
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.assertEqual(
            _sql(self.path, "SELECT name, sql FROM sqlite_master ORDER BY name"), before
        )
        self.assertEqual(len(_journal(self.path)), LATEST_VERSION)

    def test_scope_xor_check(self) -> None:
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO users (id, telegram_user_id, created_at) "
                "VALUES (1, 9940001, '2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO places (id, created_at) "
                "VALUES (1, '2026-01-01 00:00:00')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                # Обе области сразу — запрещено.
                connection.execute(
                    "INSERT INTO curator_entries (place_id, owner_user_id, "
                    "value_plain, created_at, updated_at) VALUES "
                    "(1, 1, '@both', '2026-01-01', '2026-01-01')"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                # Ни одной области — тоже запрещено.
                connection.execute(
                    "INSERT INTO curator_entries (value_plain, created_at, "
                    "updated_at) VALUES ('@none', '2026-01-01', '2026-01-01')"
                )
            connection.execute(
                "INSERT INTO curator_entries (place_id, value_plain, "
                "created_at, updated_at) VALUES "
                "(1, '@place', '2026-01-01', '2026-01-01')"
            )
            connection.execute(
                "INSERT INTO curator_entries (owner_user_id, value_plain, "
                "created_at, updated_at) VALUES "
                "(1, '@personal', '2026-01-01', '2026-01-01')"
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(
            _sql(
                self.path,
                "SELECT place_id, owner_user_id, value_plain FROM curator_entries "
                "ORDER BY id",
            ),
            [(1, None, "@place"), (None, 1, "@personal")],
        )

    def test_footer_name_is_unique_inside_scope_only(self) -> None:
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO users (id, telegram_user_id, created_at) "
                "VALUES (1, 9940001, '2026-01-01 00:00:00'), "
                "(2, 9940002, '2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO places (id, created_at) "
                "VALUES (1, '2026-01-01 00:00:00'), (2, '2026-01-01 00:00:00')"
            )
            statement = (
                "INSERT INTO footer_entries (place_id, owner_user_id, name, "
                "name_search, text_plain, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'текст', '2026-01-01', '2026-01-01')"
            )
            connection.execute(statement, (1, None, "Вход", "вход"))
            connection.execute(statement, (2, None, "Вход", "вход"))
            connection.execute(statement, (None, 1, "Вход", "вход"))
            connection.execute(statement, (None, 2, "Вход", "вход"))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(statement, (1, None, "вход", "вход"))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(statement, (None, 1, "ВХОД", "вход"))
            connection.rollback()
        finally:
            connection.close()


# --- слой доступа -----------------------------------------------------------


class _CatalogCase(unittest.TestCase):
    """Временная база, пользователи, площадка с админом и суперадмин."""

    def setUp(self) -> None:
        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,)),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.path = temp_db_path("poster-26-b1-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.applied = run_migrations(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session: Session = self.Session()
        self.addCleanup(self.session.close)

        self.owner = self.make_user(OWNER_TG, "owner")
        self.other = self.make_user(OTHER_TG, "other")
        self.admin = self.make_user(ADMIN_TG, "admin")
        self.stranger = self.make_user(STRANGER_TG, "stranger")
        self.place = self.make_place("Дом культуры", self.owner.id)
        self.other_place = self.make_place("Клуб", self.other.id)
        self.make_admin(self.place, self.admin)
        self.make_admin(self.other_place, self.other)

    def make_user(self, telegram_user_id: int, username: str) -> User:
        return get_or_create_user(
            self.session,
            telegram_user_id,
            username=username,
            full_name=f"Пользователь {telegram_user_id}",
        )

    def make_place(self, name: str, created_by_user_id: int) -> Place:
        return create_place(
            self.session,
            name,
            city="Тестовый город",
            created_by_user_id=created_by_user_id,
        )

    def make_admin(self, place: Place, user: User) -> PlaceAdmin:
        row = PlaceAdmin(place_id=place.id, user_id=user.id, assigned_at=utcnow())
        self.session.add(row)
        self.session.commit()
        return row

    # --- помощники сценариев ---------------------------------------------

    def add_curator(
        self, value: Any, *, place_id: int | None
    ) -> store.CuratorEntryView:
        return store.create_curator(
            self.session,
            actor_telegram_user_id=ADMIN_TG,
            place_id=place_id,
            value=value,
        )

    def add_footer(
        self, name: str, text: Any, *, place_id: int | None
    ) -> store.FooterEntryView:
        return store.create_footer(
            self.session,
            actor_telegram_user_id=ADMIN_TG,
            place_id=place_id,
            name=name,
            text=text,
        )


class CuratorStoreTests(_CatalogCase):
    def test_place_and_personal_curators_are_created_and_read(self) -> None:
        place_entry = self.add_curator("@place_nick", place_id=self.place.id)
        personal_entry = store.create_curator(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            place_id=None,
            value="личный контакт",
        )
        self.assertEqual(place_entry.place_id, self.place.id)
        self.assertIsNone(place_entry.owner_user_id)
        self.assertIsNone(personal_entry.place_id)
        self.assertEqual(personal_entry.owner_user_id, self.other.id)
        self.assertTrue(place_entry.is_active)

        card = store.get_curator(
            self.session,
            actor_telegram_user_id=ADMIN_TG,
            place_id=self.place.id,
            entry_id=place_entry.id,
        )
        self.assertEqual(card, place_entry)
        personal_card = store.get_curator(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            place_id=None,
            entry_id=personal_entry.id,
        )
        self.assertEqual(personal_card, personal_entry)

    def test_edit_preserves_entities_links_and_line_breaks(self) -> None:
        text = "Аня\n@anya_curator"
        entities = [
            MessageEntity(type="bold", offset=0, length=3),
            MessageEntity(type="text_link", offset=4, length=12, url="https://t.me/a"),
        ]
        created = self.add_curator(capture_text(text, entities), place_id=self.place.id)
        # Основной текст — plain-источник истины, перенос строки сохранён.
        self.assertEqual(created.value_plain, text)
        self.assertIn("Аня", created.html)
        self.assertIn('href="https://t.me/a"', created.html)
        self.assertIn("<b>", created.html)
        self.assertIn("\n", created.value_plain)

        edited = store.update_curator(
            self.session,
            actor_telegram_user_id=ADMIN_TG,
            place_id=self.place.id,
            entry_id=created.id,
            value=capture_text(
                "https://example.com/anya",
                [MessageEntity(type="url", offset=0, length=23)],
            ),
        )
        self.assertEqual(edited.id, created.id)
        self.assertEqual(edited.value_plain, "https://example.com/anya")
        self.assertNotIn("<b>", edited.html)
        # Повторное чтение видит сохранённое форматирование.
        card = store.get_curator(
            self.session,
            actor_telegram_user_id=ADMIN_TG,
            place_id=self.place.id,
            entry_id=created.id,
        )
        self.assertEqual(card.value_plain, "https://example.com/anya")
        self.assertEqual(card.html, edited.html)

    def test_plain_text_is_html_escaped_without_technical_markup(self) -> None:
        entry = self.add_curator('Миша <b> & "кавычки"', place_id=self.place.id)
        self.assertIsNone(entry.value_html, "без entities HTML не дублируется")
        self.assertEqual(entry.html, "Миша &lt;b&gt; &amp; &quot;кавычки&quot;")
        self.assertNotIn("<b>", entry.html)

    def test_archive_restore_without_physical_delete(self) -> None:
        entry = self.add_curator("@arch", place_id=self.place.id)
        archived = store.archive_curator(
            self.session,
            actor_telegram_user_id=ADMIN_TG,
            place_id=self.place.id,
            entry_id=entry.id,
        )
        self.assertTrue(archived.is_archived)
        self.assertEqual(archived.id, entry.id)
        self.assertEqual(
            store.list_curators(
                self.session,
                actor_telegram_user_id=ADMIN_TG,
                place_id=self.place.id,
            ).total,
            0,
            "архивный куратор не в активном списке",
        )
        self.assertEqual(
            store.list_curators(
                self.session,
                actor_telegram_user_id=ADMIN_TG,
                place_id=self.place.id,
                status=store.STATUS_ARCHIVED,
            ).total,
            1,
        )
        # Строка физически на месте.
        self.assertEqual(
            _sql(
                self.path,
                "SELECT status FROM curator_entries WHERE id = ?",
                (entry.id,),
            ),
            [("archived",)],
        )
        restored = store.restore_curator(
            self.session,
            actor_telegram_user_id=ADMIN_TG,
            place_id=self.place.id,
            entry_id=entry.id,
        )
        self.assertTrue(restored.is_active)
        self.assertEqual(
            store.list_curators(
                self.session,
                actor_telegram_user_id=ADMIN_TG,
                place_id=self.place.id,
            ).total,
            1,
        )

    def test_archive_twice_and_restore_active_are_rejected(self) -> None:
        entry = self.add_curator("@twice", place_id=self.place.id)
        store.archive_curator(
            self.session,
            actor_telegram_user_id=ADMIN_TG,
            place_id=self.place.id,
            entry_id=entry.id,
        )
        with self.assertRaises(store.CatalogError):
            store.archive_curator(
                self.session,
                actor_telegram_user_id=ADMIN_TG,
                place_id=self.place.id,
                entry_id=entry.id,
            )
        store.restore_curator(
            self.session,
            actor_telegram_user_id=ADMIN_TG,
            place_id=self.place.id,
            entry_id=entry.id,
        )
        with self.assertRaises(store.CatalogError):
            store.restore_curator(
                self.session,
                actor_telegram_user_id=ADMIN_TG,
                place_id=self.place.id,
                entry_id=entry.id,
            )

    def test_archived_curator_cannot_be_edited(self) -> None:
        entry = self.add_curator("@edit_arch", place_id=self.place.id)
        store.archive_curator(
            self.session,
            actor_telegram_user_id=ADMIN_TG,
            place_id=self.place.id,
            entry_id=entry.id,
        )
        with self.assertRaises(store.CatalogError):
            store.update_curator(
                self.session,
                actor_telegram_user_id=ADMIN_TG,
                place_id=self.place.id,
                entry_id=entry.id,
                value="@new",
            )

    def test_pagination_uses_five_per_page(self) -> None:
        for index in range(7):
            self.add_curator(f"@c{index}", place_id=self.place.id)
        first = store.list_curators(
            self.session, actor_telegram_user_id=ADMIN_TG, place_id=self.place.id
        )
        self.assertEqual(PAGE_SIZE, 5)
        self.assertEqual(first.total, 7)
        self.assertEqual(first.total_pages, 2)
        self.assertEqual(first.page, 0)
        self.assertEqual(len(first.items), 5)
        second = store.list_curators(
            self.session,
            actor_telegram_user_id=ADMIN_TG,
            place_id=self.place.id,
            page=1,
        )
        self.assertEqual(len(second.items), 2)
        self.assertEqual(
            {row.id for row in first.items} & {row.id for row in second.items}, set()
        )
        # Номер страницы за пределами безопасно приводится к последней.
        clamped = store.list_curators(
            self.session,
            actor_telegram_user_id=ADMIN_TG,
            place_id=self.place.id,
            page=99,
        )
        self.assertEqual(clamped.page, 1)


class FooterStoreTests(_CatalogCase):
    def test_footer_stores_name_and_text_and_substitutes_text(self) -> None:
        text = "Вход со двора,\nслева от сцены"
        footer = self.add_footer(
            "Как найти",
            capture_text(text, [MessageEntity(type="italic", offset=0, length=4)]),
            place_id=self.place.id,
        )
        self.assertEqual(footer.name, "Как найти")
        self.assertEqual(footer.text_plain, text)
        self.assertIn("<i>Вход</i>", footer.html)
        # В анонс идёт текст, а не название.
        self.assertIn("слева от сцены", footer.html)
        self.assertNotIn("Как найти", footer.html)

    def test_name_is_unique_case_insensitively_inside_scope(self) -> None:
        self.add_footer("Вход", "текст", place_id=self.place.id)
        with self.assertRaises(store.CatalogConflictError):
            self.add_footer("вход", "другой", place_id=self.place.id)
        # Другая площадка и личный справочник — независимые области.
        other_admin_footer = store.create_footer(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            place_id=self.other_place.id,
            name="Вход",
            text="текст",
        )
        self.assertEqual(other_admin_footer.name, "Вход")
        personal = store.create_footer(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            place_id=None,
            name="Вход",
            text="текст",
        )
        self.assertEqual(personal.owner_user_id, self.other.id)

    def test_rename_and_edit_text_keep_id(self) -> None:
        footer = self.add_footer("Старое", "старый текст", place_id=self.place.id)
        renamed = store.update_footer_name(
            self.session,
            actor_telegram_user_id=ADMIN_TG,
            place_id=self.place.id,
            entry_id=footer.id,
            name="Новое",
        )
        self.assertEqual((renamed.id, renamed.name), (footer.id, "Новое"))
        edited = store.update_footer_text(
            self.session,
            actor_telegram_user_id=ADMIN_TG,
            place_id=self.place.id,
            entry_id=footer.id,
            text=capture_text(
                "новый\nтекст", [MessageEntity(type="bold", offset=0, length=5)]
            ),
        )
        self.assertEqual(edited.id, footer.id)
        self.assertEqual(edited.text_plain, "новый\nтекст")
        self.assertIn("<b>новый</b>", edited.html)

    def test_archive_and_restore_footer(self) -> None:
        footer = self.add_footer("Архив", "текст", place_id=self.place.id)
        store.archive_footer(
            self.session,
            actor_telegram_user_id=ADMIN_TG,
            place_id=self.place.id,
            entry_id=footer.id,
        )
        self.assertEqual(
            store.list_footers(
                self.session,
                actor_telegram_user_id=ADMIN_TG,
                place_id=self.place.id,
            ).total,
            0,
        )
        # Название архивной записи остаётся занятым: восстановление не даёт дубль.
        with self.assertRaises(store.CatalogConflictError):
            self.add_footer("Архив", "ещё", place_id=self.place.id)
        store.restore_footer(
            self.session,
            actor_telegram_user_id=ADMIN_TG,
            place_id=self.place.id,
            entry_id=footer.id,
        )
        self.assertEqual(
            store.list_footers(
                self.session,
                actor_telegram_user_id=ADMIN_TG,
                place_id=self.place.id,
            ).total,
            1,
        )

    def test_pagination_footer(self) -> None:
        for index in range(6):
            self.add_footer(f"Подвал {index}", "текст", place_id=self.place.id)
        first = store.list_footers(
            self.session, actor_telegram_user_id=ADMIN_TG, place_id=self.place.id
        )
        self.assertEqual((first.total, first.total_pages, len(first.items)), (6, 2, 5))
        second = store.list_footers(
            self.session,
            actor_telegram_user_id=ADMIN_TG,
            place_id=self.place.id,
            page=1,
        )
        self.assertEqual(len(second.items), 1)


class CatalogPermissionTests(_CatalogCase):
    def test_non_admin_cannot_manage_place_catalog(self) -> None:
        with self.assertRaises(store.CatalogPermissionError):
            store.create_curator(
                self.session,
                actor_telegram_user_id=STRANGER_TG,
                place_id=self.place.id,
                value="@hack",
            )
        with self.assertRaises(store.CatalogPermissionError):
            store.list_curators(
                self.session,
                actor_telegram_user_id=STRANGER_TG,
                place_id=self.place.id,
            )

    def test_admin_of_another_place_is_forbidden(self) -> None:
        # OTHER_TG — админ другой площадки, не этой.
        with self.assertRaises(store.CatalogPermissionError):
            store.create_curator(
                self.session,
                actor_telegram_user_id=OTHER_TG,
                place_id=self.place.id,
                value="@foreign",
            )
        with self.assertRaises(store.CatalogPermissionError):
            store.create_footer(
                self.session,
                actor_telegram_user_id=OTHER_TG,
                place_id=self.place.id,
                name="Чужой",
                text="текст",
            )

    def test_superadmin_manages_any_place(self) -> None:
        entry = store.create_curator(
            self.session,
            actor_telegram_user_id=SUPERADMIN_TG,
            place_id=self.place.id,
            value="@super",
        )
        self.assertEqual(entry.place_id, self.place.id)

    def test_personal_catalog_belongs_only_to_owner(self) -> None:
        entry = store.create_curator(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            place_id=None,
            value="@mine",
        )
        self.assertEqual(entry.owner_user_id, self.owner.id)
        # Чужой владелец не видит и не меняет запись даже зная её ID.
        with self.assertRaises(store.CatalogPermissionError):
            store.get_curator(
                self.session,
                actor_telegram_user_id=OTHER_TG,
                place_id=None,
                entry_id=entry.id,
            )
        with self.assertRaises(store.CatalogPermissionError):
            store.update_curator(
                self.session,
                actor_telegram_user_id=OTHER_TG,
                place_id=None,
                entry_id=entry.id,
                value="@stolen",
            )
        # Администратор площадки не получает доступ к личному справочнику.
        with self.assertRaises(store.CatalogPermissionError):
            store.archive_curator(
                self.session,
                actor_telegram_user_id=ADMIN_TG,
                place_id=None,
                entry_id=entry.id,
            )
        # Испорченная запись не видна другому: его список пуст.
        self.assertEqual(
            store.list_curators(
                self.session, actor_telegram_user_id=OTHER_TG, place_id=None
            ).total,
            0,
        )

    def test_unregistered_user_has_no_personal_catalog(self) -> None:
        with self.assertRaises(store.CatalogPermissionError):
            store.create_curator(
                self.session,
                actor_telegram_user_id=1_234_567,
                place_id=None,
                value="@ghost",
            )


class CatalogIsolationTests(_CatalogCase):
    def test_places_and_users_do_not_mix(self) -> None:
        place_entry = self.add_curator("@for_place", place_id=self.place.id)
        other_entry = store.create_curator(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            place_id=self.other_place.id,
            value="@for_other_place",
        )
        personal_owner = store.create_curator(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            place_id=None,
            value="@owner_personal",
        )
        personal_other = store.create_curator(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            place_id=None,
            value="@other_personal",
        )

        place_items = store.list_curators(
            self.session, actor_telegram_user_id=ADMIN_TG, place_id=self.place.id
        ).items
        self.assertEqual([row.id for row in place_items], [place_entry.id])
        other_items = store.list_curators(
            self.session, actor_telegram_user_id=OTHER_TG, place_id=self.other_place.id
        ).items
        self.assertEqual([row.id for row in other_items], [other_entry.id])

        owner_items = store.list_curators(
            self.session, actor_telegram_user_id=OWNER_TG, place_id=None
        ).items
        self.assertEqual([row.id for row in owner_items], [personal_owner.id])
        other_personal_items = store.list_curators(
            self.session, actor_telegram_user_id=OTHER_TG, place_id=None
        ).items
        self.assertEqual([row.id for row in other_personal_items], [personal_other.id])

        # Запись другой области не открывается через чужой ID.
        with self.assertRaises(store.CatalogNotFoundError):
            store.get_curator(
                self.session,
                actor_telegram_user_id=ADMIN_TG,
                place_id=self.place.id,
                entry_id=other_entry.id,
            )
        # Подвалы изолированы так же.
        self.add_footer("Вход", "наш", place_id=self.place.id)
        store.create_footer(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            place_id=self.other_place.id,
            name="Вход",
            text="чужой",
        )
        self.assertEqual(
            store.count_footers(
                self.session, actor_telegram_user_id=ADMIN_TG, place_id=self.place.id
            ),
            1,
        )
        self.assertEqual(
            store.count_footers(
                self.session,
                actor_telegram_user_id=OTHER_TG,
                place_id=self.other_place.id,
            ),
            1,
        )


class CatalogValidationTests(_CatalogCase):
    def test_empty_values_are_rejected(self) -> None:
        with self.assertRaises(store.CatalogError):
            self.add_curator("   ", place_id=self.place.id)
        with self.assertRaises(store.CatalogError):
            self.add_footer("Название", "  \n ", place_id=self.place.id)
        with self.assertRaises(store.CatalogError):
            self.add_footer("   ", "текст", place_id=self.place.id)

    def test_unknown_status_is_rejected(self) -> None:
        with self.assertRaises(store.CatalogError):
            store.list_curators(
                self.session,
                actor_telegram_user_id=ADMIN_TG,
                place_id=self.place.id,
                status="deleted",
            )

    def test_missing_place_is_reported(self) -> None:
        with self.assertRaises(store.CatalogNotFoundError):
            store.create_curator(
                self.session,
                actor_telegram_user_id=SUPERADMIN_TG,
                place_id=999_999,
                value="@nowhere",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
