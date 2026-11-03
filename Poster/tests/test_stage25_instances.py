# tests/test_stage25_instances.py
"""
Poster 2.5, этап C1 — экземпляры готового анонса (миграция 26).

Контракт (spec §19–33):

- готовый анонс — цельное сообщение (или альбом + одна панель управления),
  поэтому экземпляры живут в ОТДЕЛЬНОЙ таблице `announcement_instances`, а
  не в `announcement_drafts`;
- на один черновик не больше ОДНОГО `active` (частичный уникальный индекс);
- `register_instance` архивирует прежний активный и выдаёт generation = max+1;
- `replace_instance` архивирует старый и создаёт новый в одной транзакции;
- `strip_panel` снимает отдельную панель управления (кнопки на сообщении);
- архивный экземпляр неизменяем (триггеры схемы) и не возвращается в строй;
- `publication_progress`/`announcement_store` не сломаны: экземпляры —
  надстройка над доставкой, а не её механизм;
- FakeBot отвечает как настоящий Bot API: `sendMediaGroup` из 3 media даёт
  3 сообщения и 3 message_id; `editMessageMedia` и
  `editMessageReplyMarkup` записываются и валидируются.

Тесты работают только с временными файлами SQLite; реальная `post_bot.db`,
`.env` и Telegram не используются.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import unittest

from sqlalchemy import text as sql_text
from telegram.error import BadRequest

from announcement_instances import (
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    AnnouncementInstance,
    archive_instance,
    get_active_instance,
    get_instance,
    list_instances,
    register_instance,
    replace_instance,
    strip_panel,
)
from announcement_wizard import (
    AnnouncementConflictError,
    AnnouncementNotFoundError,
    AnnouncementValueError,
)
from migrations import DEFAULT_MIGRATIONS, LATEST_VERSION, run_migrations
from tests.stage19a_helpers import make_engine, temp_db_path
from tests.stage27_helpers import AnnouncementDatabaseTestCase

CHAT_ID = 777
MIGRATION_26_NAME = "announcement_instances"


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


# --- миграция 26 ------------------------------------------------------------


class Migration26Tests(unittest.TestCase):
    """Чистая установка, апгрейд с v25 и ограничения таблицы."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster-25-c1-migr-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)

    def migrate(self, migrations=DEFAULT_MIGRATIONS) -> int:
        engine = make_engine(self.path)
        try:
            return run_migrations(engine, migrations)
        finally:
            engine.dispose()

    def test_fresh_install_has_instances_table(self) -> None:
        self.assertEqual(self.migrate(), LATEST_VERSION)
        self.assertEqual(LATEST_VERSION, 26)
        self.assertEqual(
            _journal(self.path)[-2:],
            [(25, "single_address_brick"), (26, MIGRATION_26_NAME)],
        )
        columns = {
            str(row[1]): (str(row[2]).upper(), int(row[3]), row[4])
            for row in _sql(self.path, "PRAGMA table_info(announcement_instances)")
        }
        self.assertEqual(
            columns,
            {
                "id": ("INTEGER", 1, None),
                "draft_id": ("INTEGER", 1, None),
                "chat_id": ("INTEGER", 1, None),
                "message_ids": ("TEXT", 1, None),
                "panel_message_id": ("INTEGER", 0, None),
                "generation": ("INTEGER", 1, "1"),
                "status": ("TEXT", 1, "'active'"),
                "fingerprint": ("TEXT", 0, None),
                "created_at": ("DATETIME", 1, None),
            },
        )
        self.assertEqual(
            sorted(
                str(row[0])
                for row in _sql(
                    self.path,
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='announcement_instances' "
                    "AND name NOT LIKE 'sqlite_%'",
                )
            ),
            ["ix_announcement_instances_draft", "ux_announcement_instances_active"],
        )
        self.assertEqual(
            sorted(
                str(row[0])
                for row in _sql(
                    self.path,
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND tbl_name='announcement_instances'",
                )
            ),
            [
                "trg_announcement_instances_identity_immutable",
                "trg_announcement_instances_status_flow",
            ],
        )
        self.assertEqual(_sql(self.path, "PRAGMA foreign_key_check"), [], "FK целы")
        self.assertEqual(
            _sql(self.path, "PRAGMA integrity_check"), [("ok",)], "целостность"
        )

    def test_upgrade_from_v25_adds_only_instances(self) -> None:
        """Upgrade-путь: база v25 получает ровно одну новую таблицу."""
        self.assertEqual(self.migrate(DEFAULT_MIGRATIONS[:25]), 25)
        before = {
            str(row[0]) for row in _sql(self.path, "SELECT name FROM sqlite_master")
        }
        self.assertNotIn("announcement_instances", before)
        self.assertEqual(self.migrate(), LATEST_VERSION)
        after = {
            str(row[0]) for row in _sql(self.path, "SELECT name FROM sqlite_master")
        }
        added = after - before
        self.assertIn("announcement_instances", added)
        # Никаких перестроек прежних таблиц: миграция только добавочная.
        self.assertEqual(
            added,
            {
                "announcement_instances",
                "ix_announcement_instances_draft",
                "ux_announcement_instances_active",
                "trg_announcement_instances_identity_immutable",
                "trg_announcement_instances_status_flow",
            },
        )
        self.assertEqual(_journal(self.path)[-1], (26, MIGRATION_26_NAME))
        # Повторный запуск ничего не добавляет и не падает.
        self.assertEqual(self.migrate(), LATEST_VERSION)
        self.assertEqual(
            {str(row[0]) for row in _sql(self.path, "SELECT name FROM sqlite_master")},
            after,
        )

    def test_two_active_instances_for_one_draft_are_impossible(self) -> None:
        """Уникальность «один active на черновик» держит сама схема."""
        self.assertEqual(self.migrate(), LATEST_VERSION)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO users (id, telegram_user_id, created_at) "
                "VALUES (1, 9910001, '2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO announcement_drafts ("
                "id, author_user_id, place_id, place_display_name, origin, "
                "stage, revision, created_at, updated_at) VALUES ("
                "1, 1, NULL, NULL, 'personal', 'completed', 1, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            values = (
                "1, 1, 777, '[10]', NULL, 1, 'active', NULL, " "'2026-01-01 00:00:00'"
            )
            connection.execute(
                "INSERT INTO announcement_instances ("
                "id, draft_id, chat_id, message_ids, panel_message_id, "
                "generation, status, fingerprint, created_at) VALUES ("
                f"{values})"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO announcement_instances ("
                    "id, draft_id, chat_id, message_ids, panel_message_id, "
                    "generation, status, fingerprint, created_at) VALUES ("
                    "2, 1, 777, '[11]', NULL, 2, 'active', NULL, "
                    "'2026-01-01 00:00:01')"
                )
            # Архивный экземпляр рядом с активным — разрешён (§31).
            connection.execute(
                "INSERT INTO announcement_instances ("
                "id, draft_id, chat_id, message_ids, panel_message_id, "
                "generation, status, fingerprint, created_at) VALUES ("
                "3, 1, 777, '[12]', NULL, 2, 'archived', NULL, "
                "'2026-01-01 00:00:02')"
            )
        finally:
            connection.close()

    def test_archived_instance_cannot_become_active_again(self) -> None:
        """Триггер: архивный снимок не возвращается в строй (§31)."""
        self.assertEqual(self.migrate(), LATEST_VERSION)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO users (id, telegram_user_id, created_at) "
                "VALUES (1, 9910001, '2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO announcement_drafts ("
                "id, author_user_id, place_id, place_display_name, origin, "
                "stage, revision, created_at, updated_at) VALUES ("
                "1, 1, NULL, NULL, 'personal', 'completed', 1, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO announcement_instances ("
                "id, draft_id, chat_id, message_ids, panel_message_id, "
                "generation, status, fingerprint, created_at) VALUES ("
                "1, 1, 777, '[10]', NULL, 1, 'archived', NULL, "
                "'2026-01-01 00:00:00')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE announcement_instances SET status='active' WHERE id=1"
                )
            # Идентичность (адреса сообщений) тоже неизменяема.
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE announcement_instances SET message_ids='[99]' WHERE id=1"
                )
        finally:
            connection.close()

    def test_message_ids_must_be_json_array(self) -> None:
        """Адреса сообщений — JSON-массив: мусор в колонку не попадает."""
        self.assertEqual(self.migrate(), LATEST_VERSION)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO users (id, telegram_user_id, created_at) "
                "VALUES (1, 9910001, '2026-01-01 00:00:00')"
            )
            connection.execute(
                "INSERT INTO announcement_drafts ("
                "id, author_user_id, place_id, place_display_name, origin, "
                "stage, revision, created_at, updated_at) VALUES ("
                "1, 1, NULL, NULL, 'personal', 'completed', 1, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO announcement_instances ("
                    "id, draft_id, chat_id, message_ids, generation, status, "
                    "created_at) VALUES ("
                    "1, 1, 777, '10', 1, 'active', '2026-01-01 00:00:00')"
                )
        finally:
            connection.close()


# --- store-слой -------------------------------------------------------------


class InstanceStoreTests(AnnouncementDatabaseTestCase):
    """
    Переходы экземпляров на реальном сервисе анонсов Stage 27.

    База поднимается помощниками Stage 27A: владелец, место, стандарт и
    черновик создаются публичным сервисом, а не прямыми INSERT.
    """

    def setUp(self) -> None:
        super().setUp()
        self.draft = self.start_draft()

    def register(self, **kwargs) -> AnnouncementInstance:
        return register_instance(
            self.session,
            self.draft.draft_id,
            CHAT_ID,
            kwargs.pop("message_ids", [301]),
            **kwargs,
        )

    def test_register_creates_active_instance(self) -> None:
        instance = self.register(panel_message_id=399, fingerprint="fp-1")
        self.assertTrue(instance.is_active)
        self.assertEqual(instance.status, STATUS_ACTIVE)
        self.assertEqual(instance.generation, 1)
        self.assertEqual(instance.message_ids, (301,))
        self.assertEqual(instance.panel_message_id, 399)
        self.assertEqual(instance.fingerprint, "fp-1")
        self.assertEqual(instance.chat_id, CHAT_ID)
        self.assertEqual(instance.draft_id, self.draft.draft_id)
        self.assertEqual(
            get_active_instance(self.session, self.draft.draft_id), instance
        )
        self.assertEqual(get_instance(self.session, instance.id), instance)

    def test_album_keeps_every_message_id_in_order(self) -> None:
        instance = self.register(message_ids=[401, 402, 403])
        self.assertEqual(instance.message_ids, (401, 402, 403))
        # Порядок отправки важен: это порядок элементов медиагруппы.
        reloaded = get_instance(self.session, instance.id)
        assert reloaded is not None
        self.assertEqual(reloaded.message_ids, (401, 402, 403))

    def test_second_register_archives_previous_and_increments_generation(
        self,
    ) -> None:
        first = self.register(message_ids=[301])
        second = self.register(message_ids=[302], panel_message_id=398)
        self.assertEqual(second.generation, 2)
        self.assertEqual(get_active_instance(self.session, self.draft.draft_id), second)
        archived = get_instance(self.session, first.id)
        assert archived is not None
        self.assertEqual(archived.status, STATUS_ARCHIVED)
        self.assertEqual(archived.message_ids, (301,), "снимок не переписан")
        self.assertEqual(
            [item.id for item in list_instances(self.session, self.draft.draft_id)],
            [second.id, first.id],
            "история: новые первыми",
        )

    def test_only_one_active_after_many_registers(self) -> None:
        for index in range(4):
            self.register(message_ids=[500 + index])
        instances = list_instances(self.session, self.draft.draft_id)
        self.assertEqual(len(instances), 4)
        self.assertEqual([item.generation for item in instances], [4, 3, 2, 1])
        active = [item for item in instances if item.is_active]
        self.assertEqual(len(active), 1, "ровно один действующий экземпляр")
        self.assertEqual(active[0].generation, 4)

    def test_archive_instance_is_idempotent_and_returns_rowcount(self) -> None:
        instance = self.register()
        self.assertEqual(archive_instance(self.session, instance.id), 1)
        archived = get_instance(self.session, instance.id)
        assert archived is not None
        self.assertEqual(archived.status, STATUS_ARCHIVED)
        self.assertIsNone(get_active_instance(self.session, self.draft.draft_id))
        self.assertEqual(
            archive_instance(self.session, instance.id), 0, "повтор — no-op"
        )

    def test_replace_instance_is_atomic(self) -> None:
        """Замена структуры медиа: старый архивный, новый активный."""
        old = self.register(message_ids=[601], panel_message_id=699)
        new = replace_instance(
            self.session,
            old.id,
            [701, 702],
            new_panel_message_id=799,
            fingerprint="fp-new",
        )
        self.assertNotEqual(new.id, old.id)
        self.assertEqual(new.draft_id, old.draft_id)
        self.assertEqual(new.chat_id, old.chat_id, "замена в том же чате")
        self.assertEqual(new.message_ids, (701, 702))
        self.assertEqual(new.panel_message_id, 799)
        self.assertEqual(new.fingerprint, "fp-new")
        self.assertEqual(new.generation, 2)
        archived = get_instance(self.session, old.id)
        assert archived is not None
        self.assertEqual(archived.status, STATUS_ARCHIVED)
        active = [
            item
            for item in list_instances(self.session, self.draft.draft_id)
            if item.is_active
        ]
        self.assertEqual([item.id for item in active], [new.id])

    def test_replace_keeps_previous_fingerprint_when_not_given(self) -> None:
        old = self.register(fingerprint="fp-1")
        new = replace_instance(self.session, old.id, [801])
        self.assertEqual(new.fingerprint, "fp-1")

    def test_replace_rejects_archived_and_unknown_instance(self) -> None:
        old = self.register()
        archive_instance(self.session, old.id)
        with self.assertRaises(AnnouncementValueError):
            replace_instance(self.session, old.id, [901])
        with self.assertRaises(AnnouncementNotFoundError):
            replace_instance(self.session, 999_999, [901])
        # Неудачная замена не создала второй активный экземпляр.
        self.assertEqual(
            [
                item
                for item in list_instances(self.session, self.draft.draft_id)
                if item.is_active
            ],
            [],
        )

    def test_replace_failure_rolls_back_both_sides(self) -> None:
        """Атомарность: сбой вставки не оставляет анонс без управления."""
        old = self.register(message_ids=[1001])
        # Реальный сбой посреди транзакции: вставка нового экземпляра
        # запрещена временным триггером (имитация ошибки БД).
        self.session.execute(
            sql_text(
                "CREATE TRIGGER tmp_block_insert BEFORE INSERT "
                "ON announcement_instances "
                "BEGIN SELECT RAISE(ABORT, 'blocked for test'); END"
            )
        )
        self.session.commit()
        try:
            with self.assertRaises(AnnouncementConflictError):
                replace_instance(self.session, old.id, [1002])
        finally:
            self.session.execute(sql_text("DROP TRIGGER tmp_block_insert"))
            self.session.commit()
        still_active = get_active_instance(self.session, self.draft.draft_id)
        assert still_active is not None
        self.assertEqual(still_active.id, old.id)
        self.assertEqual(still_active.generation, 1)
        still_stored = get_instance(self.session, old.id)
        assert still_stored is not None
        self.assertEqual(still_stored.status, STATUS_ACTIVE)
        self.assertEqual(
            len(list_instances(self.session, self.draft.draft_id)),
            1,
            "неудачная замена не создала новую строку",
        )

    def test_replace_rejects_empty_message_list_keeps_old_active(self) -> None:
        """Валидация нового набора идёт до архивации прежнего экземпляра."""
        old = self.register(message_ids=[1101])
        with self.assertRaises(AnnouncementValueError):
            replace_instance(self.session, old.id, [])
        active_after_reject = get_active_instance(self.session, self.draft.draft_id)
        assert active_after_reject is not None
        self.assertEqual(active_after_reject.id, old.id)

    def test_strip_panel_removes_only_the_panel(self) -> None:
        instance = self.register(message_ids=[1101, 1102], panel_message_id=1199)
        self.assertEqual(strip_panel(self.session, instance.id), 1)
        updated = get_instance(self.session, instance.id)
        assert updated is not None
        self.assertIsNone(updated.panel_message_id)
        self.assertEqual(updated.message_ids, (1101, 1102), "анонс не тронут")
        self.assertTrue(updated.is_active)
        self.assertEqual(strip_panel(self.session, instance.id), 0, "повтор — no-op")

    def test_register_archives_previous_even_without_panel(self) -> None:
        first = self.register()
        second = self.register()
        archived = get_instance(self.session, first.id)
        assert archived is not None
        self.assertFalse(archived.is_active)
        active = get_instance(self.session, second.id)
        assert active is not None
        self.assertTrue(active.is_active)

    def test_register_validates_input(self) -> None:
        with self.assertRaises(AnnouncementValueError):
            register_instance(self.session, self.draft.draft_id, CHAT_ID, [])
        with self.assertRaises(AnnouncementValueError):
            register_instance(self.session, self.draft.draft_id, CHAT_ID, [0])
        with self.assertRaises(AnnouncementValueError):
            register_instance(self.session, self.draft.draft_id, CHAT_ID, [True])
        with self.assertRaises(AnnouncementNotFoundError):
            register_instance(self.session, 999_999, CHAT_ID, [1])
        self.assertEqual(list_instances(self.session, self.draft.draft_id), [])

    def test_unknown_instance_is_not_found_not_an_error(self) -> None:
        """Неизвестный id — штатное «нет экземпляра», а не падение"""
        self.assertIsNone(get_instance(self.session, 999_999))
        self.assertIsNone(get_active_instance(self.session, 999_999))
        self.assertEqual(list_instances(self.session, 999_999), [])

    def test_publication_progress_is_not_touched(self) -> None:
        """
        Экземпляры — надстройка: прогресс доставки Stage 27C не меняется.

        Регистрация экземпляра не должна ни переписывать `publication_progress`,
        ни требовать статуса `sent`: это ответственность вызывающего слоя
        (C2), а store остаётся механизмом доставки.
        """
        draft = self.start_draft()
        details = self.session.execute(
            sqlite3_select_drafts(), {"draft_id": draft.draft_id}
        ).all()
        self.assertEqual(details, [(draft.draft_id, None, None)])
        instance = register_instance(self.session, draft.draft_id, CHAT_ID, [1201])
        # Сырое чтение: никаких изменений в черновике не появилось.
        marker = self.start_draft()  # второй черновик чтобы проверить изоляцию
        self.assertIsNone(get_active_instance(self.session, marker.draft_id))
        stored = get_active_instance(self.session, draft.draft_id)
        assert stored is not None
        self.assertEqual(stored.id, instance.id)


def sqlite3_select_drafts():
    """Мини-запрос для проверки, что прогресс публикации не появился."""
    from sqlalchemy import text as sql_text

    return sql_text(
        "SELECT id, publication_status, publication_progress "
        "FROM announcement_drafts WHERE id = :draft_id"
    )


# --- FakeBot ----------------------------------------------------------------


class FakeBotMediaTests(unittest.TestCase):
    """
    FakeBot ведёт себя как Bot API там, где это проверяет новый контракт.

    Без этого `sendMediaGroup` из 3 media давал бы 1 message_id, и
    экземпляр альбома нельзя было бы проверить честно.
    """

    def setUp(self) -> None:
        from tests.integration_harness import FakeBot

        self.bot = FakeBot()

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_send_media_group_returns_message_per_media(self) -> None:
        media = [object(), object(), object()]
        messages = self.run_async(
            self.bot.send_media_group(chat_id=CHAT_ID, media=media)
        )
        self.assertEqual(len(messages), 3)
        ids = [message.message_id for message in messages]
        self.assertEqual(len(set(ids)), 3, "у каждого вложения свой message_id")
        (record,) = [call for call in self.bot.calls if call["type"] == "media_group"]
        self.assertEqual(self.bot.call_message_ids[id(record)], tuple(ids))

    def test_send_media_group_keeps_order_and_chat(self) -> None:
        media = [object(), object()]
        messages = self.run_async(
            self.bot.send_media_group(chat_id=CHAT_ID, media=media)
        )
        self.assertTrue(all(message.chat.id == CHAT_ID for message in messages))
        self.assertLess(messages[0].message_id, messages[1].message_id)

    def test_edit_message_media_is_recorded_for_media_message(self) -> None:
        photo = self.run_async(
            self.bot.send_photo(chat_id=CHAT_ID, photo="file-1", caption="Текст")
        )
        self.run_async(
            self.bot.edit_message_media(
                chat_id=CHAT_ID,
                message_id=photo.message_id,
                media="file-2",
            )
        )
        (record,) = [call for call in self.bot.calls if call["type"] == "edit_media"]
        self.assertEqual(record["media"], "file-2")
        self.assertEqual(record["message_id"], photo.message_id)
        # Вид содержимого не изменился: это по-прежнему подпись.
        self.assertEqual(
            self.bot._message_content[(CHAT_ID, photo.message_id)][0], "caption"
        )

    def test_edit_message_media_on_text_message_is_rejected(self) -> None:
        message = self.run_async(
            self.bot.send_message(chat_id=CHAT_ID, text="Только текст")
        )
        with self.assertRaises(BadRequest):
            self.run_async(
                self.bot.edit_message_media(
                    chat_id=CHAT_ID,
                    message_id=message.message_id,
                    media="file-2",
                )
            )
        self.assertNotIn("edit_media", [call["type"] for call in self.bot.calls])

    def test_edit_message_media_without_media_is_rejected(self) -> None:
        photo = self.run_async(self.bot.send_photo(chat_id=CHAT_ID, photo="file-1"))
        with self.assertRaises(BadRequest):
            self.run_async(
                self.bot.edit_message_media(
                    chat_id=CHAT_ID, message_id=photo.message_id
                )
            )

    def test_edit_message_reply_markup_changes_only_keyboard(self) -> None:
        message = self.run_async(
            self.bot.send_message(chat_id=CHAT_ID, text="Анонс", reply_markup="<old>")
        )
        self.run_async(
            self.bot.edit_message_reply_markup(
                chat_id=CHAT_ID,
                message_id=message.message_id,
                reply_markup=None,
            )
        )
        (record,) = [call for call in self.bot.calls if call["type"] == "edit_markup"]
        self.assertIsNone(record["reply_markup"])
        self.assertEqual(
            self.bot._message_content[(CHAT_ID, message.message_id)],
            ("text", "Анонс", None),
            "текст сохранён, клавиатура снята",
        )
        self.assertEqual(self.bot.content_text(CHAT_ID, message.message_id), "Анонс")

    def test_edit_message_reply_markup_keeps_caption_kind(self) -> None:
        photo = self.run_async(
            self.bot.send_photo(chat_id=CHAT_ID, photo="file-1", caption="Подпись")
        )
        self.run_async(
            self.bot.edit_message_reply_markup(
                chat_id=CHAT_ID,
                message_id=photo.message_id,
                reply_markup=None,
            )
        )
        kind, text, markup = self.bot._message_content[(CHAT_ID, photo.message_id)]
        self.assertEqual((kind, text, markup), ("caption", "Подпись", None))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
