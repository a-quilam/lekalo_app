# migrations.py
"""
Схема SQLite Poster: единая базовая инициализация 1.0.0 и механизм версий.

Механизм свой, а не Alembic: проект — однофайловая SQLite-база с полностью
ручными изменениями схемы. Alembic добавил бы зависимость, каталог версий и
autogenerate, который для SQLite всё равно требует ручной проверки. Здесь
достаточно журнала `schema_migrations` и списка DDL-шагов:

- каждая версия применяется ровно один раз (журнал версий);
- повторный запуск ничего не делает (идемпотентность);
- журнал хранит контрольную сумму DDL: изменённую задним числом версию
  механизм отказывается применять к базе, где она уже записана;
- неизвестная/более новая версия или разрыв журнала = несовместимая схема;
- все ожидающие версии применяются в ОДНОЙ транзакции SQLite
  (`BEGIN IMMEDIATE`): DDL в SQLite транзакционен, поэтому сбой посреди
  применения не оставляет полуприменённое состояние;
- `BEGIN IMMEDIATE` + `busy_timeout` защищают от конкурентной инициализации:
  второй процесс ждёт, затем видит уже применённые версии;
- перед первым ожидающим DDL существующей непустой базы автоматически
  создаётся WAL-aware копия (SQLite Backup API + integrity_check). Сбой
  копии отменяет миграцию; готовые копии не удаляются.

Релиз 1.0.0: накопительные миграции 1–33 заменены ОДНОЙ базовой версией.
`MIGRATION_1_0_0_BASELINE` создаёт с нуля всю схему (31 пользовательская
таблица с индексами, CHECK, FK и 45 триггерами; вместе с журналом
`schema_migrations` — 32 таблицы в базе) и каталог системных кирпичиков с
финальными описаниями. История невыпущенных версий не поддерживается:
база, созданная прежними миграциями, считается несовместимой.

Будущие обновления: новая версия — новая запись `Migration` с version=2, 3, …
и добавляется в `DEFAULT_MIGRATIONS`; уже применённую версию редактировать
нельзя (контрольная сумма), только добавлять новую.

Ограничение: миграции требуют файловую SQLite (не `:memory:`) — приложение
всегда работает с файлом; для временных баз тесты используют временные файлы.

Правила для будущих этапов:
- уже применённую версию нельзя редактировать — только новая;
- изменения только добавочные (CREATE TABLE/INDEX, ALTER TABLE ADD COLUMN);
- разрушительные операции требуют отдельного плана восстановления
  (docs/migrations.md).
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# Столько же, сколько у основного движка (database.SQLITE_BUSY_TIMEOUT_SECONDS).
BUSY_TIMEOUT_MS = 30_000

# Копия делается в каталоге рядом с базой: `post_bot.db.backup-...`.
# Пользовательские файлы с другими именами приложение не трогает.
BACKUP_SUFFIX = ".backup-"

_JOURNAL_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    " version INTEGER PRIMARY KEY,"
    " name TEXT NOT NULL,"
    " checksum TEXT NOT NULL,"
    " applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
    ")"
)


@dataclass(frozen=True)
class Migration:
    """
    Одна неизменяемая версия схемы: версия, имя и набор DDL/DML-шагов.

    `requires_tables` — таблицы, без которых шаги версии неприменимы. Если
    хотя бы одной из них нет, шаги пропускаются, а версия всё равно
    фиксируется в журнале: так будущая миграция остаётся идемпотентной и на
    базе, где таблица никогда не создавалась. Для базовой инициализации
    список пуст: она создаёт всё с нуля.
    """

    version: int
    name: str
    statements: tuple[str, ...]
    requires_tables: tuple[str, ...] = ()

    @property
    def checksum(self) -> str:
        """
        Контрольная сумма DDL.

        Пробельные различия (переносы строк, отступы) не считаются
        изменением; любое смысловое изменение SQL — считается.
        """
        digest = hashlib.sha256()
        for statement in self.statements:
            normalized = " ".join(statement.split())
            digest.update(normalized.encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()


class MigrationError(RuntimeError):
    """Миграция не применена; база осталась в состоянии до запуска."""


class IncompatibleSchemaError(MigrationError):
    """Схема базы несовместима с кодом (новее, изменена или с разрывом)."""


class BackupError(MigrationError):
    """Не удалось безопасно скопировать базу — миграцию начинать нельзя."""


# --- Базовая версия 1.0.0: полная схема с нуля ------------------------------
#
# DDL ниже — семантически точная копия схемы, которую к релизу 1.0.0
# создавали накопительные миграции 1–33. Порядок объектов повторяет
# исторический: сначала справочник мест, затем кирпичики, шаблоны, анонсы,
# конфликты, справочники и журнал событий; индексы и триггеры идут сразу за
# своей таблицей. Порядок важен для имён автоматических индексов
# (`sqlite_autoindex_<таблица>_N`) и для читаемости diff'а.
#
# `user_states` раньше создавалась SQLAlchemy (`Base.metadata.create_all`):
# теперь она часть базовой схемы, create_all остаётся страховкой.

_BASELINE_SCHEMA: tuple[str, ...] = (
    # table users
    """
CREATE TABLE users (
            id INTEGER NOT NULL PRIMARY KEY,
            telegram_user_id INTEGER NOT NULL,
            username VARCHAR(255),
            full_name VARCHAR(255),
            created_at DATETIME NOT NULL,
            updated_at DATETIME,
            last_seen_at DATETIME,
            CONSTRAINT uq_users_telegram_user_id UNIQUE (telegram_user_id)
        )
    """,
    # sqlite_autoindex_users_1: автоматический индекс ограничения таблицы
    # table places
    """
CREATE TABLE places (
            id INTEGER NOT NULL PRIMARY KEY,
            created_by_user_id INTEGER,
            created_at DATETIME NOT NULL, address TEXT, url TEXT, city TEXT,
            CONSTRAINT fk_places_created_by_user_id
                FOREIGN KEY (created_by_user_id) REFERENCES users (id)
                ON DELETE SET NULL
        )
    """,
    # index ix_places_created_by_user_id
    """
CREATE INDEX ix_places_created_by_user_id ON places (created_by_user_id)
    """,
    # table place_names
    """
CREATE TABLE place_names (
            id INTEGER NOT NULL PRIMARY KEY,
            place_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            name_search TEXT NOT NULL,
            is_canonical INTEGER NOT NULL,
            created_by_user_id INTEGER,
            created_at DATETIME NOT NULL,
            CONSTRAINT ck_place_names_name_not_empty
                CHECK (length(trim(name)) > 0),
            CONSTRAINT ck_place_names_search_not_empty
                CHECK (length(trim(name_search)) > 0),
            CONSTRAINT ck_place_names_is_canonical
                CHECK (is_canonical IN (0, 1)),
            CONSTRAINT uq_place_names_place_search
                UNIQUE (place_id, name_search),
            CONSTRAINT fk_place_names_place_id
                FOREIGN KEY (place_id) REFERENCES places (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_place_names_created_by_user_id
                FOREIGN KEY (created_by_user_id) REFERENCES users (id)
                ON DELETE SET NULL
        )
    """,
    # sqlite_autoindex_place_names_1: автоматический индекс ограничения таблицы
    # index ix_place_names_place_id
    """
CREATE INDEX ix_place_names_place_id ON place_names (place_id)
    """,
    # index ix_place_names_name_search
    """
CREATE INDEX ix_place_names_name_search ON place_names (name_search)
    """,
    # index ux_place_names_one_canonical
    """
CREATE UNIQUE INDEX ux_place_names_one_canonical
            ON place_names (place_id) WHERE is_canonical = 1

    """,
    # table place_sessions
    """
CREATE TABLE place_sessions (
            telegram_user_id INTEGER NOT NULL PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at DATETIME NOT NULL
        )
    """,
    # table place_admins
    """
CREATE TABLE place_admins (
            id INTEGER NOT NULL PRIMARY KEY,
            place_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            assigned_by_user_id INTEGER,
            assigned_at DATETIME NOT NULL,
            revoked_by_user_id INTEGER,
            revoked_at DATETIME,
            CONSTRAINT fk_place_admins_place_id
                FOREIGN KEY (place_id) REFERENCES places (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_place_admins_user_id
                FOREIGN KEY (user_id) REFERENCES users (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_place_admins_assigned_by_user_id
                FOREIGN KEY (assigned_by_user_id) REFERENCES users (id)
                ON DELETE SET NULL,
            CONSTRAINT fk_place_admins_revoked_by_user_id
                FOREIGN KEY (revoked_by_user_id) REFERENCES users (id)
                ON DELETE SET NULL
        )
    """,
    # index ix_place_admins_place_id
    """
CREATE INDEX ix_place_admins_place_id ON place_admins (place_id)
    """,
    # index ix_place_admins_user_id
    """
CREATE INDEX ix_place_admins_user_id ON place_admins (user_id)
    """,
    # index ux_place_admins_one_active
    """
CREATE UNIQUE INDEX ux_place_admins_one_active
            ON place_admins (place_id) WHERE revoked_at IS NULL

    """,
    # table admin_sessions
    """
CREATE TABLE admin_sessions (
            telegram_user_id INTEGER NOT NULL PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at DATETIME NOT NULL
        )
    """,
    # table place_edit_log
    """
CREATE TABLE place_edit_log (
            id INTEGER NOT NULL PRIMARY KEY,
            place_id INTEGER NOT NULL,
            actor_user_id INTEGER,
            action TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            created_at DATETIME NOT NULL,
            CONSTRAINT fk_place_edit_log_place_id
                FOREIGN KEY (place_id)
                REFERENCES places (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_place_edit_log_actor_user_id
                FOREIGN KEY (actor_user_id)
                REFERENCES users (id)
                ON DELETE SET NULL
        )
    """,
    # index ix_place_edit_log_place_id
    """
CREATE INDEX ix_place_edit_log_place_id ON place_edit_log (place_id)
    """,
    # table admin_transfers
    """
CREATE TABLE admin_transfers (
            id INTEGER NOT NULL PRIMARY KEY,
            place_id INTEGER NOT NULL,
            from_user_id INTEGER NOT NULL,
            from_assignment_id INTEGER,
            to_user_id INTEGER,
            opened_by_user_id INTEGER,
            to_username VARCHAR(255),
            token_hash VARCHAR(64),
            status TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            expires_at DATETIME NOT NULL,
            opened_at DATETIME,
            confirmed_at DATETIME,
            closed_at DATETIME,
            notified_at DATETIME,
            CONSTRAINT ck_admin_transfers_status
                CHECK (status IN (
                    'pending', 'confirmed', 'rejected', 'cancelled',
                    'expired', 'superseded'
                )),
            CONSTRAINT fk_admin_transfers_place_id
                FOREIGN KEY (place_id) REFERENCES places (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_admin_transfers_from_user_id
                FOREIGN KEY (from_user_id) REFERENCES users (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_admin_transfers_from_assignment_id
                FOREIGN KEY (from_assignment_id) REFERENCES place_admins (id)
                ON DELETE SET NULL,
            CONSTRAINT fk_admin_transfers_to_user_id
                FOREIGN KEY (to_user_id) REFERENCES users (id)
                ON DELETE SET NULL,
            CONSTRAINT fk_admin_transfers_opened_by_user_id
                FOREIGN KEY (opened_by_user_id) REFERENCES users (id)
                ON DELETE SET NULL
        )
    """,
    # index ix_admin_transfers_place_id
    """
CREATE INDEX ix_admin_transfers_place_id ON admin_transfers (place_id)
    """,
    # index ix_admin_transfers_from_user_id
    """
CREATE INDEX ix_admin_transfers_from_user_id ON admin_transfers (from_user_id)
    """,
    # index ix_admin_transfers_to_user_id
    """
CREATE INDEX ix_admin_transfers_to_user_id ON admin_transfers (to_user_id)
    """,
    # index ux_admin_transfers_one_pending_place
    """
CREATE UNIQUE INDEX ux_admin_transfers_one_pending_place
            ON admin_transfers (place_id) WHERE status = 'pending'

    """,
    # index ix_admin_transfers_pending_to_user
    """
CREATE INDEX ix_admin_transfers_pending_to_user
            ON admin_transfers (to_user_id) WHERE status = 'pending'

    """,
    # index ux_admin_transfers_token_hash
    """
CREATE UNIQUE INDEX ux_admin_transfers_token_hash
            ON admin_transfers (token_hash) WHERE token_hash IS NOT NULL

    """,
    # table bricks
    """
CREATE TABLE bricks (
            id INTEGER NOT NULL PRIMARY KEY,
            code TEXT NOT NULL,
            title TEXT NOT NULL,
            type TEXT NOT NULL,
            scope TEXT NOT NULL,
            source TEXT NOT NULL,
            place_id INTEGER,
            owner_user_id INTEGER,
            rules_json TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_by_user_id INTEGER,
            created_at DATETIME NOT NULL,
            updated_at DATETIME, revision INTEGER NOT NULL DEFAULT 1, description TEXT,
            CONSTRAINT ck_bricks_type
                CHECK (type IN (
                    'short_text', 'long_text', 'date', 'time', 'url',
                    'number', 'choice'
                )),
            CONSTRAINT ck_bricks_scope
                CHECK (scope IN ('system', 'place', 'personal')),
            CONSTRAINT ck_bricks_source
                CHECK (source IN ('ask', 'place', 'context')),
            CONSTRAINT ck_bricks_status
                CHECK (status IN ('active', 'archived')),
            CONSTRAINT ck_bricks_scope_owner
                CHECK (
                    (scope = 'system'
                        AND place_id IS NULL AND owner_user_id IS NULL)
                    OR (scope = 'place'
                        AND place_id IS NOT NULL AND owner_user_id IS NULL)
                    OR (scope = 'personal'
                        AND place_id IS NULL AND owner_user_id IS NOT NULL)
                ),
            CONSTRAINT ck_bricks_code_length
                CHECK (length(code) BETWEEN 2 AND 64),
            CONSTRAINT ck_bricks_code_trimmed
                CHECK (code = trim(code)),
            CONSTRAINT ck_bricks_code_no_space
                CHECK (instr(code, ' ') = 0),
            CONSTRAINT ck_bricks_code_no_brackets
                CHECK (instr(code, '[') = 0 AND instr(code, ']') = 0),
            CONSTRAINT ck_bricks_no_photo_code
                CHECK (code <> 'фото'),
            CONSTRAINT ck_bricks_title_not_empty
                CHECK (length(trim(title)) > 0),
            CONSTRAINT ck_bricks_rules_json_object
                CHECK (
                    rules_json IS NULL
                    OR (json_valid(rules_json)
                        AND json_type(rules_json) = 'object')
                ),
            CONSTRAINT fk_bricks_place_id
                FOREIGN KEY (place_id) REFERENCES places (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_bricks_owner_user_id
                FOREIGN KEY (owner_user_id) REFERENCES users (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_bricks_created_by_user_id
                FOREIGN KEY (created_by_user_id) REFERENCES users (id)
                ON DELETE SET NULL
        )
    """,
    # index ux_bricks_system_code
    """
CREATE UNIQUE INDEX ux_bricks_system_code
            ON bricks (code) WHERE scope = 'system'

    """,
    # index ux_bricks_place_code
    """
CREATE UNIQUE INDEX ux_bricks_place_code
            ON bricks (place_id, code) WHERE scope = 'place'

    """,
    # index ux_bricks_personal_code
    """
CREATE UNIQUE INDEX ux_bricks_personal_code
            ON bricks (owner_user_id, code) WHERE scope = 'personal'

    """,
    # index ix_bricks_scope_status_code
    """
CREATE INDEX ix_bricks_scope_status_code
            ON bricks (scope, status, code)

    """,
    # index ix_bricks_place_status
    """
CREATE INDEX ix_bricks_place_status ON bricks (place_id, status)

    """,
    # index ix_bricks_owner_status
    """
CREATE INDEX ix_bricks_owner_status ON bricks (owner_user_id, status)

    """,
    # index ix_bricks_type_status
    """
CREATE INDEX ix_bricks_type_status ON bricks (type, status)

    """,
    # index ix_bricks_code
    """
CREATE INDEX ix_bricks_code ON bricks (code)
    """,
    # index ix_bricks_title
    """
CREATE INDEX ix_bricks_title ON bricks (title)
    """,
    # trigger trg_bricks_id_immutable
    """
CREATE TRIGGER trg_bricks_id_immutable
        BEFORE UPDATE OF id ON bricks
        FOR EACH ROW
        WHEN NEW.id <> OLD.id
        BEGIN
            SELECT RAISE(ABORT, 'bricks.id is immutable');
        END
    """,
    # trigger trg_bricks_system_code_immutable
    """
CREATE TRIGGER trg_bricks_system_code_immutable
        BEFORE UPDATE OF code ON bricks
        FOR EACH ROW
        WHEN OLD.scope = 'system' AND NEW.code <> OLD.code
        BEGIN
            SELECT RAISE(ABORT, 'system brick code is immutable');
        END
    """,
    # trigger trg_bricks_system_owner_immutable
    """
CREATE TRIGGER trg_bricks_system_owner_immutable
        BEFORE UPDATE OF scope, place_id, owner_user_id ON bricks
        FOR EACH ROW
        WHEN OLD.scope = 'system'
            AND (NEW.scope <> 'system'
                OR NEW.place_id IS NOT NULL
                OR NEW.owner_user_id IS NOT NULL)
        BEGIN
            SELECT RAISE(ABORT, 'system brick is global');
        END
    """,
    # table brick_sessions
    """
CREATE TABLE brick_sessions (
            telegram_user_id INTEGER NOT NULL PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT ck_brick_sessions_payload_object CHECK (
                json_valid(payload) AND json_type(payload) = 'object'
            )
        )
    """,
    # trigger trg_bricks_revision_increases
    """
CREATE TRIGGER trg_bricks_revision_increases
        BEFORE UPDATE OF revision ON bricks
        FOR EACH ROW
        WHEN NEW.revision <= OLD.revision
        BEGIN
            SELECT RAISE(ABORT, 'brick revision must increase');
        END
    """,
    # table templates
    """
CREATE TABLE templates (
            id INTEGER NOT NULL PRIMARY KEY,
            kind TEXT NOT NULL,
            place_id INTEGER,
            owner_user_id INTEGER,
            name TEXT,
            revision INTEGER NOT NULL DEFAULT 1,
            created_by_user_id INTEGER,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL, extended_active INTEGER NOT NULL DEFAULT 0
            CONSTRAINT ck_templates_extended_active
            CHECK (extended_active IN (0, 1)),
            CONSTRAINT ck_templates_kind
                CHECK (kind IN ('standard', 'personal')),
            CONSTRAINT ck_templates_shape
                CHECK (
                    (kind = 'standard'
                        AND place_id IS NOT NULL
                        AND owner_user_id IS NULL
                        AND name IS NULL)
                    OR (kind = 'personal'
                        AND place_id IS NULL
                        AND owner_user_id IS NOT NULL
                        AND length(trim(name)) > 0)
                ),
            CONSTRAINT ck_templates_revision CHECK (revision >= 1),
            CONSTRAINT fk_templates_place_id
                FOREIGN KEY (place_id) REFERENCES places (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_templates_owner_user_id
                FOREIGN KEY (owner_user_id) REFERENCES users (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_templates_created_by_user_id
                FOREIGN KEY (created_by_user_id) REFERENCES users (id)
                ON DELETE SET NULL
        )
    """,
    # index ux_templates_one_standard_place
    """
CREATE UNIQUE INDEX ux_templates_one_standard_place
            ON templates (place_id) WHERE kind = 'standard'

    """,
    # index ix_templates_owner_kind
    """
CREATE INDEX ix_templates_owner_kind ON templates (owner_user_id, kind)
    """,
    # index ix_templates_place_kind
    """
CREATE INDEX ix_templates_place_kind ON templates (place_id, kind)
    """,
    # trigger trg_templates_identity_immutable
    """
CREATE TRIGGER trg_templates_identity_immutable
        BEFORE UPDATE OF kind, place_id, owner_user_id ON templates
        FOR EACH ROW
        WHEN NEW.kind <> OLD.kind
            OR NEW.place_id IS NOT OLD.place_id
            OR NEW.owner_user_id IS NOT OLD.owner_user_id
        BEGIN
            SELECT RAISE(ABORT, 'template identity is immutable');
        END
    """,
    # trigger trg_templates_revision_increases
    """
CREATE TRIGGER trg_templates_revision_increases
        BEFORE UPDATE OF revision ON templates
        FOR EACH ROW
        WHEN NEW.revision <= OLD.revision
        BEGIN
            SELECT RAISE(ABORT, 'template revision must increase');
        END
    """,
    # trigger trg_templates_standard_no_delete
    """
CREATE TRIGGER trg_templates_standard_no_delete
        BEFORE DELETE ON templates
        FOR EACH ROW
        WHEN OLD.kind = 'standard'
            AND EXISTS (SELECT 1 FROM places WHERE id = OLD.place_id)
        BEGIN
            SELECT RAISE(ABORT, 'standard template cannot be deleted');
        END
    """,
    # table template_sessions
    """
CREATE TABLE template_sessions (
            telegram_user_id INTEGER NOT NULL PRIMARY KEY,
            payload TEXT NOT NULL CHECK (
                json_valid(payload) AND json_type(payload) = 'object'
            ),
            updated_at DATETIME NOT NULL
        )
    """,
    # table brick_proposals
    """
CREATE TABLE brick_proposals (
            id INTEGER NOT NULL PRIMARY KEY,
            author_user_id INTEGER NOT NULL,
            scope TEXT NOT NULL,
            place_id INTEGER,
            source_brick_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            code TEXT NOT NULL,
            type TEXT NOT NULL,
            source TEXT NOT NULL,
            rules_json TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at DATETIME NOT NULL,
            decided_at DATETIME,
            decided_by_user_id INTEGER,
            approved_brick_id INTEGER,
            decision_note TEXT, description TEXT,
            CONSTRAINT ck_brick_proposals_scope
                CHECK (scope IN ('place', 'system')),
            CONSTRAINT ck_brick_proposals_target
                CHECK (
                    (scope = 'place' AND place_id IS NOT NULL)
                    OR (scope = 'system' AND place_id IS NULL)
                ),
            CONSTRAINT ck_brick_proposals_status
                CHECK (status IN ('pending', 'approved', 'rejected', 'withdrawn')),
            CONSTRAINT ck_brick_proposals_type
                CHECK (type IN (
                    'short_text', 'long_text', 'date', 'time', 'url',
                    'number', 'choice'
                )),
            CONSTRAINT ck_brick_proposals_source
                CHECK (source IN ('ask', 'place', 'context')),
            CONSTRAINT ck_brick_proposals_code_length
                CHECK (length(code) BETWEEN 2 AND 64),
            CONSTRAINT ck_brick_proposals_code_trimmed
                CHECK (code = trim(code)),
            CONSTRAINT ck_brick_proposals_code_no_space
                CHECK (instr(code, ' ') = 0),
            CONSTRAINT ck_brick_proposals_code_no_brackets
                CHECK (instr(code, '[') = 0 AND instr(code, ']') = 0),
            CONSTRAINT ck_brick_proposals_no_photo_code
                CHECK (code <> 'фото'),
            CONSTRAINT ck_brick_proposals_title_not_empty
                CHECK (length(trim(title)) > 0),
            CONSTRAINT ck_brick_proposals_rules_json_object
                CHECK (
                    rules_json IS NULL
                    OR (json_valid(rules_json)
                        AND json_type(rules_json) = 'object')
                ),
            CONSTRAINT ck_brick_proposals_decision_shape
                CHECK (
                    (status = 'pending'
                        AND decided_at IS NULL
                        AND decided_by_user_id IS NULL
                        AND approved_brick_id IS NULL
                        AND decision_note IS NULL)
                    OR (status = 'approved'
                        AND decided_at IS NOT NULL
                        AND decided_by_user_id IS NOT NULL
                        AND approved_brick_id IS NOT NULL
                        AND decision_note IS NULL)
                    OR (status = 'rejected'
                        AND decided_at IS NOT NULL
                        AND decided_by_user_id IS NOT NULL
                        AND approved_brick_id IS NULL)
                    OR (status = 'withdrawn'
                        AND decided_at IS NOT NULL
                        AND decided_by_user_id IS NOT NULL
                        AND approved_brick_id IS NULL
                        AND decision_note IS NULL)
                ),
            CONSTRAINT fk_brick_proposals_author
                FOREIGN KEY (author_user_id) REFERENCES users (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_brick_proposals_place
                FOREIGN KEY (place_id) REFERENCES places (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_brick_proposals_source_brick
                FOREIGN KEY (source_brick_id) REFERENCES bricks (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_brick_proposals_decided_by
                FOREIGN KEY (decided_by_user_id) REFERENCES users (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_brick_proposals_approved_brick
                FOREIGN KEY (approved_brick_id) REFERENCES bricks (id)
                ON DELETE RESTRICT
        )
    """,
    # index ix_brick_proposals_author_status_created
    """
CREATE INDEX ix_brick_proposals_author_status_created
            ON brick_proposals (author_user_id, status, created_at)

    """,
    # index ix_brick_proposals_place_status_created
    """
CREATE INDEX ix_brick_proposals_place_status_created
            ON brick_proposals (place_id, status, created_at)

    """,
    # index ix_brick_proposals_status_scope_created
    """
CREATE INDEX ix_brick_proposals_status_scope_created
            ON brick_proposals (status, scope, created_at)

    """,
    # index ix_brick_proposals_source_brick
    """
CREATE INDEX ix_brick_proposals_source_brick
            ON brick_proposals (source_brick_id)

    """,
    # index ux_brick_proposals_approved_brick
    """
CREATE UNIQUE INDEX ux_brick_proposals_approved_brick
            ON brick_proposals (approved_brick_id)
            WHERE approved_brick_id IS NOT NULL

    """,
    # trigger trg_brick_proposals_single_transition
    """
CREATE TRIGGER trg_brick_proposals_single_transition
        BEFORE UPDATE OF status ON brick_proposals
        FOR EACH ROW
        WHEN OLD.status <> 'pending'
            OR NEW.status NOT IN ('approved', 'rejected', 'withdrawn')
        BEGIN
            SELECT RAISE(ABORT, 'brick proposal decision is final');
        END
    """,
    # trigger trg_brick_proposals_source_owner
    """
CREATE TRIGGER trg_brick_proposals_source_owner
        BEFORE INSERT ON brick_proposals
        FOR EACH ROW
        WHEN NOT EXISTS (
            SELECT 1 FROM bricks b
            WHERE b.id = NEW.source_brick_id
              AND b.scope = 'personal'
              AND b.owner_user_id = NEW.author_user_id
              AND b.status = 'active'
        )
        BEGIN
            SELECT RAISE(ABORT, 'brick proposal source must be an active personal brick');
        END
    """,
    # table announcement_values
    """
CREATE TABLE announcement_values (
            id INTEGER NOT NULL PRIMARY KEY,
            draft_id INTEGER NOT NULL,
            brick_id INTEGER NOT NULL,
            state TEXT NOT NULL,
            value_json TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT ck_announcement_values_state
                CHECK (state IN ('valid', 'empty_optional')),
            CONSTRAINT ck_announcement_values_payload
                CHECK (
                    (state = 'valid'
                        AND value_json IS NOT NULL
                        AND json_valid(value_json)
                        AND json_type(value_json) = 'object')
                    OR (state = 'empty_optional' AND value_json IS NULL)
                ),
            CONSTRAINT fk_announcement_values_draft
                FOREIGN KEY (draft_id) REFERENCES announcement_drafts (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_announcement_values_brick
                FOREIGN KEY (brick_id) REFERENCES bricks (id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_announcement_values_draft_brick
                UNIQUE (draft_id, brick_id)
        )
    """,
    # sqlite_autoindex_announcement_values_1: автоматический индекс ограничения таблицы
    # index ix_announcement_values_brick
    """
CREATE INDEX ix_announcement_values_brick
            ON announcement_values (brick_id)

    """,
    # table announcement_drafts
    """
CREATE TABLE announcement_drafts (
            id INTEGER NOT NULL PRIMARY KEY,
            author_user_id INTEGER NOT NULL,
            place_id INTEGER,
            place_name_id INTEGER,
            place_display_name TEXT,
            place_address TEXT,
            place_url TEXT,
            origin TEXT NOT NULL DEFAULT 'place',
            template_id INTEGER,
            template_version INTEGER,
            template_snapshot_id INTEGER,
            stage TEXT NOT NULL DEFAULT 'place',
            revision INTEGER NOT NULL DEFAULT 1,
            idempotency_key TEXT,
            photos TEXT,
            publication_status TEXT,
            publication_text TEXT,
            publication_photos TEXT,
            publication_progress TEXT,
            publication_error TEXT,
            publication_started_at DATETIME,
            published_at DATETIME,
            live_message_ids_json TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL, place_city TEXT
            CONSTRAINT ck_announcement_drafts_city
            CHECK (place_city IS NULL OR length(trim(place_city)) > 0),
            CONSTRAINT ck_announcement_drafts_stage
                CHECK (stage IN (
                    'place', 'template', 'questions', 'extras',
                    'preview', 'completed'
                )),
            CONSTRAINT ck_announcement_drafts_revision
                CHECK (revision >= 1),
            CONSTRAINT ck_announcement_drafts_origin
                CHECK (origin IN ('place', 'personal')),
            CONSTRAINT ck_announcement_drafts_origin_place
                CHECK (
                    (origin = 'place'
                        AND place_id IS NOT NULL
                        AND place_display_name IS NOT NULL
                        AND length(trim(place_display_name)) > 0)
                    OR (origin = 'personal'
                        AND place_id IS NULL
                        AND place_display_name IS NULL)
                ),
            CONSTRAINT ck_announcement_drafts_address
                CHECK (place_address IS NULL OR length(trim(place_address)) > 0),
            CONSTRAINT ck_announcement_drafts_url
                CHECK (place_url IS NULL OR length(trim(place_url)) > 0),
            CONSTRAINT ck_announcement_drafts_idempotency
                CHECK (
                    idempotency_key IS NULL
                    OR length(trim(idempotency_key)) > 0
                ),
            CONSTRAINT ck_announcement_drafts_template_shape
                CHECK (
                    (template_id IS NULL
                        AND template_version IS NULL
                        AND template_snapshot_id IS NULL)
                    OR (template_id IS NOT NULL
                        AND template_version IS NOT NULL
                        AND template_version >= 1
                        AND template_snapshot_id IS NOT NULL)
                ),
            CONSTRAINT ck_announcement_drafts_live_ids
                CHECK (
                    live_message_ids_json IS NULL
                    OR (json_valid(live_message_ids_json)
                        AND json_type(live_message_ids_json) = 'array')
                ),
            CONSTRAINT fk_announcement_drafts_author
                FOREIGN KEY (author_user_id) REFERENCES users (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_announcement_drafts_place
                FOREIGN KEY (place_id) REFERENCES places (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_announcement_drafts_place_name
                FOREIGN KEY (place_name_id) REFERENCES place_names (id)
                ON DELETE SET NULL,
            CONSTRAINT fk_announcement_drafts_template
                FOREIGN KEY (template_id) REFERENCES templates (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_announcement_drafts_snapshot
                FOREIGN KEY (template_snapshot_id) REFERENCES template_snapshots (id)
                ON DELETE RESTRICT
        )
    """,
    # index ix_announcement_drafts_author_updated
    """
CREATE INDEX ix_announcement_drafts_author_updated
            ON announcement_drafts (author_user_id, updated_at)

    """,
    # index ux_announcement_drafts_snapshot
    """
CREATE UNIQUE INDEX ux_announcement_drafts_snapshot
            ON announcement_drafts (template_snapshot_id)
            WHERE template_snapshot_id IS NOT NULL

    """,
    # index ux_announcement_drafts_idempotency
    """
CREATE UNIQUE INDEX ux_announcement_drafts_idempotency
            ON announcement_drafts (author_user_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL

    """,
    # index ix_announcement_drafts_publication_status
    """
CREATE INDEX ix_announcement_drafts_publication_status
            ON announcement_drafts (publication_status)

    """,
    # trigger trg_announcement_drafts_revision_increases
    """
CREATE TRIGGER trg_announcement_drafts_revision_increases
        BEFORE UPDATE OF revision ON announcement_drafts
        FOR EACH ROW
        WHEN NEW.revision <= OLD.revision
        BEGIN
            SELECT RAISE(ABORT, 'announcement draft revision must increase');
        END
    """,
    # trigger trg_announcement_drafts_identity_immutable
    """
CREATE TRIGGER trg_announcement_drafts_identity_immutable
        BEFORE UPDATE OF
            author_user_id, place_id, idempotency_key, created_at, origin
        ON announcement_drafts
        FOR EACH ROW
        WHEN NEW.author_user_id IS NOT OLD.author_user_id
            OR NEW.place_id IS NOT OLD.place_id
            OR NEW.idempotency_key IS NOT OLD.idempotency_key
            OR NEW.created_at IS NOT OLD.created_at
            OR NEW.origin IS NOT OLD.origin
        BEGIN
            SELECT RAISE(ABORT, 'announcement draft identity is immutable');
        END
    """,
    # trigger trg_announcement_drafts_template_immutable
    """
CREATE TRIGGER trg_announcement_drafts_template_immutable
        BEFORE UPDATE OF template_id, template_version, template_snapshot_id
        ON announcement_drafts
        FOR EACH ROW
        WHEN OLD.template_snapshot_id IS NOT NULL
            AND (
                NEW.template_id IS NOT OLD.template_id
                OR NEW.template_version IS NOT OLD.template_version
                OR NEW.template_snapshot_id IS NOT OLD.template_snapshot_id
            )
        BEGIN
            SELECT RAISE(ABORT, 'announcement template snapshot is immutable');
        END
    """,
    # trigger trg_announcement_values_identity_immutable
    """
CREATE TRIGGER trg_announcement_values_identity_immutable
        BEFORE UPDATE OF draft_id, brick_id, created_at ON announcement_values
        FOR EACH ROW
        WHEN NEW.draft_id IS NOT OLD.draft_id
            OR NEW.brick_id IS NOT OLD.brick_id
            OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'announcement value identity is immutable');
        END
    """,
    # trigger trg_announcement_drafts_media_json_insert
    """
CREATE TRIGGER trg_announcement_drafts_media_json_insert
        BEFORE INSERT ON announcement_drafts
        FOR EACH ROW
        WHEN (NEW.photos IS NOT NULL AND (
                json_valid(NEW.photos) = 0
                OR CASE WHEN json_valid(NEW.photos) = 1
                        THEN json_type(NEW.photos) END <> 'array'
            ))
            OR (NEW.publication_photos IS NOT NULL AND (
                json_valid(NEW.publication_photos) = 0
                OR CASE WHEN json_valid(NEW.publication_photos) = 1
                        THEN json_type(NEW.publication_photos) END <> 'array'
            ))
            OR (NEW.publication_progress IS NOT NULL AND (
                json_valid(NEW.publication_progress) = 0
                OR CASE WHEN json_valid(NEW.publication_progress) = 1
                        THEN json_type(NEW.publication_progress) END <> 'object'
            ))
        BEGIN
            SELECT RAISE(ABORT, 'announcement media must be valid JSON');
        END
    """,
    # trigger trg_announcement_drafts_media_json_update
    """
CREATE TRIGGER trg_announcement_drafts_media_json_update
        BEFORE UPDATE OF photos, publication_photos, publication_progress
        ON announcement_drafts
        FOR EACH ROW
        WHEN (NEW.photos IS NOT NULL AND (
                json_valid(NEW.photos) = 0
                OR CASE WHEN json_valid(NEW.photos) = 1
                        THEN json_type(NEW.photos) END <> 'array'
            ))
            OR (NEW.publication_photos IS NOT NULL AND (
                json_valid(NEW.publication_photos) = 0
                OR CASE WHEN json_valid(NEW.publication_photos) = 1
                        THEN json_type(NEW.publication_photos) END <> 'array'
            ))
            OR (NEW.publication_progress IS NOT NULL AND (
                json_valid(NEW.publication_progress) = 0
                OR CASE WHEN json_valid(NEW.publication_progress) = 1
                        THEN json_type(NEW.publication_progress) END <> 'object'
            ))
        BEGIN
            SELECT RAISE(ABORT, 'announcement media must be valid JSON');
        END
    """,
    # trigger trg_announcement_drafts_publication_status_insert
    """
CREATE TRIGGER trg_announcement_drafts_publication_status_insert
        BEFORE INSERT ON announcement_drafts
        FOR EACH ROW
        WHEN NEW.publication_status IS NOT NULL
            AND NEW.publication_status NOT IN
                ('sending', 'partial', 'failed', 'sent')
        BEGIN
            SELECT RAISE(ABORT, 'unknown announcement publication status');
        END
    """,
    # trigger trg_announcement_drafts_publication_status_update
    """
CREATE TRIGGER trg_announcement_drafts_publication_status_update
        BEFORE UPDATE OF publication_status ON announcement_drafts
        FOR EACH ROW
        WHEN NEW.publication_status IS NOT NULL
            AND NEW.publication_status NOT IN
                ('sending', 'partial', 'failed', 'sent')
        BEGIN
            SELECT RAISE(ABORT, 'unknown announcement publication status');
        END
    """,
    # trigger trg_announcement_drafts_publication_shape_insert
    """
CREATE TRIGGER trg_announcement_drafts_publication_shape_insert
        BEFORE INSERT ON announcement_drafts
        FOR EACH ROW
        WHEN (
            NEW.publication_status IS NULL
            AND (
                NEW.publication_text IS NOT NULL
                OR NEW.publication_photos IS NOT NULL
                OR NEW.publication_progress IS NOT NULL
                OR NEW.publication_error IS NOT NULL
                OR NEW.publication_started_at IS NOT NULL
                OR NEW.published_at IS NOT NULL
            )
        ) OR (
            NEW.publication_status IS NOT NULL
            AND (
                NEW.publication_text IS NULL
                OR length(trim(NEW.publication_text)) = 0
                OR NEW.publication_photos IS NULL
                OR NEW.publication_started_at IS NULL
                OR (
                    NEW.publication_status IN ('sending', 'partial', 'failed')
                    AND NEW.published_at IS NOT NULL
                )
                OR (
                    NEW.publication_status = 'sent'
                    AND NEW.published_at IS NULL
                )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid announcement publication shape');
        END
    """,
    # trigger trg_announcement_drafts_publication_shape
    """
CREATE TRIGGER trg_announcement_drafts_publication_shape
        BEFORE UPDATE OF
            publication_status, publication_text, publication_photos,
            publication_progress, publication_error,
            publication_started_at, published_at
        ON announcement_drafts
        FOR EACH ROW
        WHEN (
            NEW.publication_status IS NULL
            AND (
                NEW.publication_text IS NOT NULL
                OR NEW.publication_photos IS NOT NULL
                OR NEW.publication_progress IS NOT NULL
                OR NEW.publication_error IS NOT NULL
                OR NEW.publication_started_at IS NOT NULL
                OR NEW.published_at IS NOT NULL
            )
        ) OR (
            NEW.publication_status IS NOT NULL
            AND (
                NEW.publication_text IS NULL
                OR length(trim(NEW.publication_text)) = 0
                OR NEW.publication_photos IS NULL
                OR NEW.publication_started_at IS NULL
                OR (
                    NEW.publication_status IN ('sending', 'partial', 'failed')
                    AND NEW.published_at IS NOT NULL
                )
                OR (
                    NEW.publication_status = 'sent'
                    AND NEW.published_at IS NULL
                )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid announcement publication shape');
        END
    """,
    # trigger trg_announcement_drafts_published_immutable
    """
CREATE TRIGGER trg_announcement_drafts_published_immutable
        BEFORE UPDATE OF
            publication_status, publication_text, publication_photos,
            publication_progress, publication_error,
            publication_started_at, published_at
        ON announcement_drafts
        FOR EACH ROW
        WHEN OLD.publication_status = 'sent'
            AND NOT (
                NEW.publication_status = 'sending'
                AND NEW.published_at IS NULL
                AND NEW.publication_started_at IS NOT NULL
                AND NEW.publication_text IS NOT NULL
                AND length(trim(NEW.publication_text)) > 0
                AND NEW.publication_photos IS NOT NULL
                AND NEW.publication_error IS NULL
                AND NEW.publication_progress IS NOT NULL
                AND (NEW.publication_progress -> '$.previous_ids')
                    IS (OLD.publication_progress -> '$.ids')
            )
        BEGIN
            SELECT RAISE(ABORT, 'announcement already delivered');
        END
    """,
    # table brick_conflicts
    """
CREATE TABLE brick_conflicts (
            id INTEGER NOT NULL PRIMARY KEY,
            code TEXT NOT NULL,
            place_id INTEGER NOT NULL,
            system_brick_id INTEGER NOT NULL,
            local_brick_id INTEGER NOT NULL,
            proposal_id INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            created_at DATETIME NOT NULL,
            resolved_at DATETIME,
            resolved_by_user_id INTEGER,
            resolution_note TEXT,
            CONSTRAINT ck_brick_conflicts_status
                CHECK (status IN ('open', 'renamed', 'switched', 'superseded')),
            CONSTRAINT ck_brick_conflicts_code_length
                CHECK (length(code) BETWEEN 2 AND 64),
            CONSTRAINT ck_brick_conflicts_shape
                CHECK (
                    (status = 'open'
                        AND resolved_at IS NULL
                        AND resolved_by_user_id IS NULL)
                    OR (status IN ('renamed', 'switched')
                        AND resolved_at IS NOT NULL
                        AND resolved_by_user_id IS NOT NULL)
                    OR (status = 'superseded'
                        AND resolved_at IS NOT NULL
                        AND resolved_by_user_id IS NULL)
                ),
            CONSTRAINT fk_brick_conflicts_place
                FOREIGN KEY (place_id) REFERENCES places (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_brick_conflicts_system_brick
                FOREIGN KEY (system_brick_id) REFERENCES bricks (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_brick_conflicts_local_brick
                FOREIGN KEY (local_brick_id) REFERENCES bricks (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_brick_conflicts_proposal
                FOREIGN KEY (proposal_id) REFERENCES brick_proposals (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_brick_conflicts_resolved_by
                FOREIGN KEY (resolved_by_user_id) REFERENCES users (id)
                ON DELETE RESTRICT
        )
    """,
    # index ux_brick_conflicts_system_local
    """
CREATE UNIQUE INDEX ux_brick_conflicts_system_local
            ON brick_conflicts (system_brick_id, local_brick_id)

    """,
    # index ix_brick_conflicts_place_status
    """
CREATE INDEX ix_brick_conflicts_place_status ON brick_conflicts (place_id, status)
    """,
    # index ix_brick_conflicts_local_brick
    """
CREATE INDEX ix_brick_conflicts_local_brick ON brick_conflicts (local_brick_id)
    """,
    # table brick_conflict_notifications
    """
CREATE TABLE brick_conflict_notifications (
            id INTEGER NOT NULL PRIMARY KEY,
            conflict_id INTEGER NOT NULL,
            telegram_user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL,
            sent_at DATETIME,
            CONSTRAINT ck_brick_conflict_notifications_status
                CHECK (status IN ('pending', 'sent', 'failed')),
            CONSTRAINT ck_brick_conflict_notifications_attempts
                CHECK (attempts >= 0),
            CONSTRAINT ck_brick_conflict_notifications_shape
                CHECK (
                    (status = 'pending' AND sent_at IS NULL)
                    OR (status IN ('sent', 'failed') AND sent_at IS NOT NULL)
                ),
            CONSTRAINT fk_brick_conflict_notifications_conflict
                FOREIGN KEY (conflict_id) REFERENCES brick_conflicts (id)
                ON DELETE CASCADE
        )
    """,
    # index ux_brick_conflict_notifications_target
    """
CREATE UNIQUE INDEX ux_brick_conflict_notifications_target
            ON brick_conflict_notifications (conflict_id, telegram_user_id)

    """,
    # index ix_brick_conflict_notifications_status
    """
CREATE INDEX ix_brick_conflict_notifications_status
            ON brick_conflict_notifications (status, created_at)

    """,
    # trigger trg_brick_conflicts_single_transition
    """
CREATE TRIGGER trg_brick_conflicts_single_transition
        BEFORE UPDATE OF status ON brick_conflicts
        FOR EACH ROW
        WHEN OLD.status <> 'open'
            OR NEW.status NOT IN ('renamed', 'switched', 'superseded')
        BEGIN
            SELECT RAISE(ABORT, 'brick conflict decision is final');
        END
    """,
    # trigger trg_brick_conflicts_decision_immutable
    """
CREATE TRIGGER trg_brick_conflicts_decision_immutable
        BEFORE UPDATE ON brick_conflicts
        FOR EACH ROW
        WHEN OLD.status <> 'open'
            AND (
                NEW.status IS NOT OLD.status
                OR NEW.resolved_at IS NOT OLD.resolved_at
                OR NEW.resolved_by_user_id IS NOT OLD.resolved_by_user_id
                OR NEW.resolution_note IS NOT OLD.resolution_note
            )
        BEGIN
            SELECT RAISE(ABORT, 'brick conflict decision is immutable');
        END
    """,
    # trigger trg_brick_conflicts_snapshot_immutable
    """
CREATE TRIGGER trg_brick_conflicts_snapshot_immutable
        BEFORE UPDATE OF
            code, place_id, system_brick_id, local_brick_id, proposal_id,
            created_at
        ON brick_conflicts
        FOR EACH ROW
        WHEN NEW.code IS NOT OLD.code
            OR NEW.place_id IS NOT OLD.place_id
            OR NEW.system_brick_id IS NOT OLD.system_brick_id
            OR NEW.local_brick_id IS NOT OLD.local_brick_id
            OR NEW.proposal_id IS NOT OLD.proposal_id
            OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'brick conflict snapshot is immutable');
        END
    """,
    # trigger trg_brick_conflict_notifications_single_transition
    """
CREATE TRIGGER trg_brick_conflict_notifications_single_transition
        BEFORE UPDATE OF status ON brick_conflict_notifications
        FOR EACH ROW
        WHEN OLD.status <> 'pending' OR NEW.status = 'pending'
        BEGIN
            SELECT RAISE(ABORT, 'brick conflict notification is final');
        END
    """,
    # trigger trg_brick_proposals_snapshot_immutable
    """
CREATE TRIGGER trg_brick_proposals_snapshot_immutable
        BEFORE UPDATE OF
            author_user_id, scope, place_id, source_brick_id, title, code,
            type, source, rules_json, description, created_at
        ON brick_proposals
        FOR EACH ROW
        WHEN NEW.author_user_id IS NOT OLD.author_user_id
            OR NEW.scope IS NOT OLD.scope
            OR NEW.place_id IS NOT OLD.place_id
            OR NEW.source_brick_id IS NOT OLD.source_brick_id
            OR NEW.title IS NOT OLD.title
            OR NEW.code IS NOT OLD.code
            OR NEW.type IS NOT OLD.type
            OR NEW.source IS NOT OLD.source
            OR NEW.rules_json IS NOT OLD.rules_json
            OR NEW.description IS NOT OLD.description
            OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'brick proposal snapshot is immutable');
        END
    """,
    # table place_invite_links
    """
CREATE TABLE place_invite_links (
            id INTEGER NOT NULL PRIMARY KEY,
            token VARCHAR(32) NOT NULL,
            place_id INTEGER NOT NULL,
            created_by_user_id INTEGER,
            created_at DATETIME NOT NULL,
            revoked_at DATETIME,
            CONSTRAINT fk_place_invite_links_place_id
                FOREIGN KEY (place_id) REFERENCES places (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_place_invite_links_created_by_user_id
                FOREIGN KEY (created_by_user_id) REFERENCES users (id)
                ON DELETE SET NULL,
            CONSTRAINT ck_place_invite_links_token_not_empty
                CHECK (length(trim(token)) > 0)
        )
    """,
    # index ux_place_invite_links_token
    """
CREATE UNIQUE INDEX ux_place_invite_links_token ON place_invite_links (token)
    """,
    # index ix_place_invite_links_place_id
    """
CREATE INDEX ix_place_invite_links_place_id ON place_invite_links (place_id)
    """,
    # index ux_place_invite_links_one_active
    """
CREATE UNIQUE INDEX ux_place_invite_links_one_active
            ON place_invite_links (place_id) WHERE revoked_at IS NULL

    """,
    # table template_versions
    """
CREATE TABLE template_versions (
            id INTEGER NOT NULL PRIMARY KEY,
            template_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            content TEXT NOT NULL,
            extended_content TEXT,
            created_by_user_id INTEGER,
            created_at DATETIME NOT NULL,
            builtin_version INTEGER,
            CONSTRAINT uq_template_versions_template_version
                UNIQUE (template_id, version),
            CONSTRAINT ck_template_versions_version CHECK (version >= 1),
            CONSTRAINT ck_template_versions_builtin_version
                CHECK (builtin_version IS NULL OR builtin_version >= 1),
            CONSTRAINT ck_template_versions_content_not_empty
                CHECK (content <> ''),
            CONSTRAINT ck_template_versions_extended_not_empty
                CHECK (extended_content IS NULL OR extended_content <> ''),
            CONSTRAINT fk_template_versions_template_id
                FOREIGN KEY (template_id) REFERENCES templates (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_template_versions_created_by_user_id
                FOREIGN KEY (created_by_user_id) REFERENCES users (id)
                ON DELETE SET NULL
        )
    """,
    # sqlite_autoindex_template_versions_1: автоматический индекс ограничения таблицы
    # table template_version_bricks
    """
CREATE TABLE template_version_bricks (
            template_version_id INTEGER NOT NULL,
            brick_id INTEGER NOT NULL,
            code TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (template_version_id, brick_id),
            CONSTRAINT fk_template_version_bricks_version
                FOREIGN KEY (template_version_id)
                REFERENCES template_versions (id) ON DELETE CASCADE,
            CONSTRAINT fk_template_version_bricks_brick
                FOREIGN KEY (brick_id) REFERENCES bricks (id)
                ON DELETE RESTRICT
        )
    """,
    # sqlite_autoindex_template_version_bricks_1: автоматический индекс ограничения таблицы
    # table template_snapshots
    """
CREATE TABLE template_snapshots (
            id INTEGER NOT NULL PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            source_template_id INTEGER NOT NULL,
            source_template_version INTEGER NOT NULL,
            source_kind TEXT NOT NULL,
            source_name TEXT,
            source_place_id INTEGER,
            source_owner_user_id INTEGER,
            content TEXT NOT NULL,
            extended_content TEXT,
            builtin_version INTEGER,
            idempotency_key TEXT,
            created_at DATETIME NOT NULL, conditional_rules_frozen INTEGER NOT NULL DEFAULT 0
            CONSTRAINT ck_template_snapshots_conditional_rules_frozen
            CHECK (conditional_rules_frozen IN (0, 1)), extended_active INTEGER NOT NULL DEFAULT 0
            CONSTRAINT ck_template_snapshots_extended_active
            CHECK (extended_active IN (0, 1)),
            CONSTRAINT fk_template_snapshots_owner_user_id
                FOREIGN KEY (owner_user_id) REFERENCES users (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_template_snapshots_source_version
                FOREIGN KEY (source_template_id, source_template_version)
                REFERENCES template_versions (template_id, version)
                ON DELETE RESTRICT,
            CONSTRAINT fk_template_snapshots_source_place_id
                FOREIGN KEY (source_place_id) REFERENCES places (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_template_snapshots_source_owner_user_id
                FOREIGN KEY (source_owner_user_id) REFERENCES users (id)
                ON DELETE SET NULL,
            CONSTRAINT ck_template_snapshots_source_kind
                CHECK (source_kind IN ('standard', 'personal')),
            CONSTRAINT ck_template_snapshots_source_version
                CHECK (source_template_version >= 1),
            CONSTRAINT ck_template_snapshots_builtin_version
                CHECK (builtin_version IS NULL OR builtin_version >= 1),
            CONSTRAINT ck_template_snapshots_content_not_empty
                CHECK (content <> ''),
            CONSTRAINT ck_template_snapshots_extended_not_empty
                CHECK (extended_content IS NULL OR extended_content <> ''),
            CONSTRAINT ck_template_snapshots_idempotency_key
                CHECK (
                    idempotency_key IS NULL
                    OR length(trim(idempotency_key)) > 0
                )
        )
    """,
    # table template_snapshot_bricks
    """
CREATE TABLE template_snapshot_bricks (
            template_snapshot_id INTEGER NOT NULL,
            brick_id INTEGER NOT NULL,
            code TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (template_snapshot_id, brick_id),
            CONSTRAINT fk_template_snapshot_bricks_snapshot
                FOREIGN KEY (template_snapshot_id)
                REFERENCES template_snapshots (id) ON DELETE CASCADE,
            CONSTRAINT fk_template_snapshot_bricks_brick
                FOREIGN KEY (brick_id) REFERENCES bricks (id)
                ON DELETE RESTRICT
        )
    """,
    # sqlite_autoindex_template_snapshot_bricks_1: автоматический индекс ограничения таблицы
    # table announcement_extras
    """
CREATE TABLE announcement_extras (
            id INTEGER NOT NULL PRIMARY KEY,
            draft_id INTEGER NOT NULL,
            brick_id INTEGER NOT NULL,
            occurrence INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT ck_announcement_extras_occurrence
                CHECK (occurrence >= 1),
            CONSTRAINT fk_announcement_extras_draft
                FOREIGN KEY (draft_id) REFERENCES announcement_drafts (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_announcement_extras_brick
                FOREIGN KEY (brick_id) REFERENCES bricks (id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_announcement_extras_draft_brick_occurrence
                UNIQUE (draft_id, brick_id, occurrence)
        )
    """,
    # sqlite_autoindex_announcement_extras_1: автоматический индекс ограничения таблицы
    # index ix_template_version_bricks_brick_id
    """
CREATE INDEX ix_template_version_bricks_brick_id
            ON template_version_bricks (brick_id)

    """,
    # index ux_template_version_bricks_version_code
    """
CREATE UNIQUE INDEX ux_template_version_bricks_version_code
            ON template_version_bricks (template_version_id, code)

    """,
    # index ux_template_snapshots_idempotency
    """
CREATE UNIQUE INDEX ux_template_snapshots_idempotency
            ON template_snapshots (owner_user_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL

    """,
    # index ix_template_snapshots_owner_created
    """
CREATE INDEX ix_template_snapshots_owner_created
            ON template_snapshots (owner_user_id, created_at)

    """,
    # index ix_template_snapshots_source
    """
CREATE INDEX ix_template_snapshots_source
            ON template_snapshots (source_template_id, source_template_version)

    """,
    # index ix_template_snapshot_bricks_brick_id
    """
CREATE INDEX ix_template_snapshot_bricks_brick_id
            ON template_snapshot_bricks (brick_id)

    """,
    # index ux_template_snapshot_bricks_snapshot_code
    """
CREATE UNIQUE INDEX ux_template_snapshot_bricks_snapshot_code
            ON template_snapshot_bricks (template_snapshot_id, code)

    """,
    # index ix_announcement_extras_draft
    """
CREATE INDEX ix_announcement_extras_draft
            ON announcement_extras (draft_id)

    """,
    # index ix_announcement_extras_brick
    """
CREATE INDEX ix_announcement_extras_brick
            ON announcement_extras (brick_id)

    """,
    # trigger trg_template_versions_no_update
    """
CREATE TRIGGER trg_template_versions_no_update
        BEFORE UPDATE ON template_versions
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'template versions are immutable');
        END
    """,
    # trigger trg_template_versions_no_delete
    """
CREATE TRIGGER trg_template_versions_no_delete
        BEFORE DELETE ON template_versions
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM templates WHERE id = OLD.template_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'template versions cannot be deleted');
        END
    """,
    # trigger trg_template_version_bricks_no_update
    """
CREATE TRIGGER trg_template_version_bricks_no_update
        BEFORE UPDATE ON template_version_bricks
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'template bindings are immutable');
        END
    """,
    # trigger trg_template_snapshots_no_update
    """
CREATE TRIGGER trg_template_snapshots_no_update
        BEFORE UPDATE ON template_snapshots
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'template snapshots are immutable');
        END
    """,
    # trigger trg_template_snapshots_no_delete
    """
CREATE TRIGGER trg_template_snapshots_no_delete
        BEFORE DELETE ON template_snapshots
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'template snapshots cannot be deleted');
        END
    """,
    # trigger trg_template_snapshot_bricks_no_update
    """
CREATE TRIGGER trg_template_snapshot_bricks_no_update
        BEFORE UPDATE ON template_snapshot_bricks
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'snapshot bindings are immutable');
        END
    """,
    # trigger trg_announcement_extras_identity_immutable
    """
CREATE TRIGGER trg_announcement_extras_identity_immutable
        BEFORE UPDATE OF draft_id, brick_id, occurrence, created_at
        ON announcement_extras
        FOR EACH ROW
        WHEN NEW.draft_id IS NOT OLD.draft_id
            OR NEW.brick_id IS NOT OLD.brick_id
            OR NEW.occurrence IS NOT OLD.occurrence
            OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'announcement extra identity is immutable');
        END
    """,
    # trigger trg_bricks_system_no_delete
    """
CREATE TRIGGER trg_bricks_system_no_delete
        BEFORE DELETE ON bricks
        FOR EACH ROW
        WHEN OLD.scope = 'system'
        BEGIN
            SELECT RAISE(ABORT, 'system brick cannot be deleted');
        END
    """,
    # trigger trg_template_version_bricks_no_delete
    """
CREATE TRIGGER trg_template_version_bricks_no_delete
        BEFORE DELETE ON template_version_bricks
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM template_versions
            WHERE id = OLD.template_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'template bindings cannot be deleted');
        END
    """,
    # trigger trg_template_snapshot_bricks_no_delete
    """
CREATE TRIGGER trg_template_snapshot_bricks_no_delete
        BEFORE DELETE ON template_snapshot_bricks
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'snapshot bindings cannot be deleted');
        END
    """,
    # trigger trg_brick_conflicts_no_delete
    """
CREATE TRIGGER trg_brick_conflicts_no_delete
        BEFORE DELETE ON brick_conflicts
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'brick conflict history cannot be deleted');
        END
    """,
    # trigger trg_brick_proposals_no_delete
    """
CREATE TRIGGER trg_brick_proposals_no_delete
        BEFORE DELETE ON brick_proposals
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'brick proposal history cannot be deleted');
        END
    """,
    # trigger trg_brick_proposals_decision_immutable
    """
CREATE TRIGGER trg_brick_proposals_decision_immutable
        BEFORE UPDATE ON brick_proposals
        FOR EACH ROW
        WHEN OLD.status <> 'pending'
            AND (
                NEW.status IS NOT OLD.status
                OR NEW.decided_at IS NOT OLD.decided_at
                OR NEW.decided_by_user_id IS NOT OLD.decided_by_user_id
                OR NEW.approved_brick_id IS NOT OLD.approved_brick_id
                OR NEW.decision_note IS NOT OLD.decision_note
            )
        BEGIN
            SELECT RAISE(ABORT, 'brick proposal decision is immutable');
        END
    """,
    # trigger trg_brick_proposals_approval_link
    """
CREATE TRIGGER trg_brick_proposals_approval_link
        BEFORE UPDATE OF status, approved_brick_id ON brick_proposals
        FOR EACH ROW
        WHEN NEW.status = 'approved' AND NOT EXISTS (
            SELECT 1 FROM bricks b
            WHERE b.id = NEW.approved_brick_id
              AND b.status = 'active'
              AND b.scope = NEW.scope
              AND b.place_id IS NEW.place_id
              AND b.owner_user_id IS NULL
              AND b.code = NEW.code
              AND b.title = NEW.title
              AND b.type = NEW.type
              AND b.source = NEW.source
              AND b.rules_json IS NEW.rules_json
              AND (
                  (
                      NEW.scope = 'system'
                      AND b.id = OLD.source_brick_id
                  )
                  OR (
                      NEW.scope <> 'system'
                      AND b.id <> OLD.source_brick_id
                  )
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'approved brick does not match proposal');
        END
    """,
    # table announcement_instances
    """
CREATE TABLE announcement_instances (
            id INTEGER NOT NULL PRIMARY KEY,
            draft_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            message_ids TEXT NOT NULL,
            panel_message_id INTEGER,
            generation INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            fingerprint TEXT,
            created_at DATETIME NOT NULL,
            CONSTRAINT ck_announcement_instances_status
                CHECK (status IN ('active', 'archived')),
            CONSTRAINT ck_announcement_instances_generation
                CHECK (generation >= 1),
            CONSTRAINT ck_announcement_instances_message_ids
                CHECK (
                    json_valid(message_ids)
                    AND json_type(message_ids) = 'array'
                ),
            CONSTRAINT ck_announcement_instances_panel
                CHECK (panel_message_id IS NULL OR panel_message_id > 0),
            CONSTRAINT fk_announcement_instances_draft
                FOREIGN KEY (draft_id) REFERENCES announcement_drafts (id)
                ON DELETE CASCADE
        )
    """,
    # index ux_announcement_instances_active
    """
CREATE UNIQUE INDEX ux_announcement_instances_active
            ON announcement_instances (draft_id) WHERE status = 'active'

    """,
    # index ix_announcement_instances_draft
    """
CREATE INDEX ix_announcement_instances_draft
            ON announcement_instances (draft_id, generation)

    """,
    # trigger trg_announcement_instances_identity_immutable
    """
CREATE TRIGGER trg_announcement_instances_identity_immutable
        BEFORE UPDATE OF draft_id, chat_id, message_ids, generation, created_at
        ON announcement_instances
        FOR EACH ROW
        WHEN NEW.draft_id IS NOT OLD.draft_id
            OR NEW.chat_id IS NOT OLD.chat_id
            OR NEW.message_ids IS NOT OLD.message_ids
            OR NEW.generation IS NOT OLD.generation
            OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'announcement instance identity is immutable');
        END
    """,
    # trigger trg_announcement_instances_status_flow
    """
CREATE TRIGGER trg_announcement_instances_status_flow
        BEFORE UPDATE OF status ON announcement_instances
        FOR EACH ROW
        WHEN OLD.status = 'archived' AND NEW.status = 'active'
        BEGIN
            SELECT RAISE(ABORT, 'archived announcement instance is immutable');
        END
    """,
    # table template_conditional_parts
    """
CREATE TABLE template_conditional_parts (
            id INTEGER NOT NULL PRIMARY KEY,
            template_id INTEGER NOT NULL,
            version_id INTEGER NOT NULL,
            section TEXT NOT NULL,
            brick_id INTEGER NOT NULL,
            fragment TEXT NOT NULL,
            occurrence INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'active',
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT ck_template_conditional_parts_section
                CHECK (section IN ('content', 'extended_content')),
            CONSTRAINT ck_template_conditional_parts_status
                CHECK (status IN ('active', 'stale')),
            CONSTRAINT ck_template_conditional_parts_occurrence
                CHECK (occurrence >= 1),
            CONSTRAINT ck_template_conditional_parts_fragment_not_empty
                CHECK (length(trim(fragment)) > 0),
            CONSTRAINT fk_template_conditional_parts_template
                FOREIGN KEY (template_id) REFERENCES templates (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_template_conditional_parts_version
                FOREIGN KEY (version_id) REFERENCES template_versions (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_template_conditional_parts_brick
                FOREIGN KEY (brick_id) REFERENCES bricks (id)
                ON DELETE RESTRICT
        )
    """,
    # index ix_template_conditional_parts_version_section
    """
CREATE INDEX ix_template_conditional_parts_version_section
            ON template_conditional_parts (version_id, section)

    """,
    # table template_snapshot_conditional_parts
    """
CREATE TABLE template_snapshot_conditional_parts (
            id INTEGER NOT NULL PRIMARY KEY,
            snapshot_id INTEGER NOT NULL,
            section TEXT NOT NULL,
            brick_id INTEGER NOT NULL,
            fragment TEXT NOT NULL,
            occurrence INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL,
            CONSTRAINT ck_template_snapshot_conditional_parts_section
                CHECK (section IN ('content', 'extended_content')),
            CONSTRAINT ck_template_snapshot_conditional_parts_occurrence
                CHECK (occurrence >= 1),
            CONSTRAINT fk_template_snapshot_conditional_parts_snapshot
                FOREIGN KEY (snapshot_id) REFERENCES template_snapshots (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_template_snapshot_conditional_parts_brick
                FOREIGN KEY (brick_id) REFERENCES bricks (id)
                ON DELETE RESTRICT
        )
    """,
    # index ix_template_snapshot_conditional_parts_snapshot_section
    """
CREATE INDEX ix_template_snapshot_conditional_parts_snapshot_section
            ON template_snapshot_conditional_parts (snapshot_id, section)

    """,
    # table user_event_log
    """
CREATE TABLE user_event_log (
            id INTEGER NOT NULL PRIMARY KEY,
            telegram_user_id INTEGER NOT NULL,
            created_at DATETIME NOT NULL,
            action TEXT NOT NULL,
            screen TEXT,
            scenario TEXT,
            result TEXT,
            detail TEXT
        )
    """,
    # index ix_user_event_log_user_created
    """
CREATE INDEX ix_user_event_log_user_created
            ON user_event_log (telegram_user_id, created_at)

    """,
    # index ix_user_event_log_created
    """
CREATE INDEX ix_user_event_log_created
            ON user_event_log (created_at)

    """,
    # table reports
    """
CREATE TABLE reports (
            id INTEGER NOT NULL PRIMARY KEY,
            author_user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            username TEXT,
            display_name TEXT,
            body TEXT NOT NULL,
            screen_label TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            events_snapshot TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            developer_reply TEXT,
            replied_at DATETIME,
            closed_at DATETIME,
            notified_user_ids TEXT,
            CONSTRAINT ck_reports_status
                CHECK (status IN ('active', 'archived'))
        )
    """,
    # index ix_reports_status_created
    """
CREATE INDEX ix_reports_status_created
            ON reports (status, created_at)

    """,
    # trigger trg_bricks_reserve_codes_insert
    """
CREATE TRIGGER trg_bricks_reserve_codes_insert
        BEFORE INSERT ON bricks
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM bricks b
            WHERE b.code = NEW.code
              AND (
                  (NEW.scope = 'system' AND b.scope <> 'system')
                  OR (NEW.scope <> 'system' AND b.scope = 'system')
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'system brick code is reserved');
        END
    """,
    # trigger trg_bricks_reserve_codes_update
    """
CREATE TRIGGER trg_bricks_reserve_codes_update
        BEFORE UPDATE OF code, scope ON bricks
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM bricks b
            WHERE b.id <> OLD.id
              AND b.code = NEW.code
              AND (
                  (NEW.scope = 'system' AND b.scope <> 'system')
                  OR (NEW.scope <> 'system' AND b.scope = 'system')
              )
        )
        AND NOT (
            NEW.code = OLD.code
            AND (
                NEW.scope = OLD.scope
                OR (OLD.scope = 'personal' AND NEW.scope = 'system')
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'system brick code is reserved');
        END
    """,
    # table curator_entries
    """
CREATE TABLE curator_entries (
            id INTEGER NOT NULL PRIMARY KEY,
            place_id INTEGER,
            owner_user_id INTEGER,
            value_plain TEXT NOT NULL,
            value_html TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_by_user_id INTEGER,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT ck_curator_entries_scope
                CHECK (
                    (place_id IS NOT NULL AND owner_user_id IS NULL)
                    OR (place_id IS NULL AND owner_user_id IS NOT NULL)
                ),
            CONSTRAINT ck_curator_entries_value_not_empty
                CHECK (length(trim(value_plain)) > 0),
            CONSTRAINT ck_curator_entries_status
                CHECK (status IN ('active', 'archived')),
            CONSTRAINT fk_curator_entries_place_id
                FOREIGN KEY (place_id) REFERENCES places (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_curator_entries_owner_user_id
                FOREIGN KEY (owner_user_id) REFERENCES users (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_curator_entries_created_by_user_id
                FOREIGN KEY (created_by_user_id) REFERENCES users (id)
                ON DELETE SET NULL
        )
    """,
    # index ix_curator_entries_place_status
    """
CREATE INDEX ix_curator_entries_place_status
            ON curator_entries (place_id, status)

    """,
    # index ix_curator_entries_owner_status
    """
CREATE INDEX ix_curator_entries_owner_status
            ON curator_entries (owner_user_id, status)

    """,
    # table footer_entries
    """
CREATE TABLE footer_entries (
            id INTEGER NOT NULL PRIMARY KEY,
            place_id INTEGER,
            owner_user_id INTEGER,
            name TEXT NOT NULL,
            name_search TEXT NOT NULL,
            text_plain TEXT NOT NULL,
            text_html TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_by_user_id INTEGER,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT ck_footer_entries_scope
                CHECK (
                    (place_id IS NOT NULL AND owner_user_id IS NULL)
                    OR (place_id IS NULL AND owner_user_id IS NOT NULL)
                ),
            CONSTRAINT ck_footer_entries_name_not_empty
                CHECK (length(trim(name)) > 0),
            CONSTRAINT ck_footer_entries_search_not_empty
                CHECK (length(trim(name_search)) > 0),
            CONSTRAINT ck_footer_entries_text_not_empty
                CHECK (length(trim(text_plain)) > 0),
            CONSTRAINT ck_footer_entries_status
                CHECK (status IN ('active', 'archived')),
            CONSTRAINT fk_footer_entries_place_id
                FOREIGN KEY (place_id) REFERENCES places (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_footer_entries_owner_user_id
                FOREIGN KEY (owner_user_id) REFERENCES users (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_footer_entries_created_by_user_id
                FOREIGN KEY (created_by_user_id) REFERENCES users (id)
                ON DELETE SET NULL
        )
    """,
    # index ux_footer_entries_place_name
    """
CREATE UNIQUE INDEX ux_footer_entries_place_name
            ON footer_entries (place_id, name_search) WHERE place_id IS NOT NULL

    """,
    # index ux_footer_entries_owner_name
    """
CREATE UNIQUE INDEX ux_footer_entries_owner_name
            ON footer_entries (owner_user_id, name_search)
            WHERE owner_user_id IS NOT NULL

    """,
    # index ix_footer_entries_place_status
    """
CREATE INDEX ix_footer_entries_place_status
            ON footer_entries (place_id, status)

    """,
    # index ix_footer_entries_owner_status
    """
CREATE INDEX ix_footer_entries_owner_status
            ON footer_entries (owner_user_id, status)

    """,
    # trigger trg_curator_entries_id_immutable
    """
CREATE TRIGGER trg_curator_entries_id_immutable
        BEFORE UPDATE OF id ON curator_entries
        FOR EACH ROW
        WHEN NEW.id <> OLD.id
        BEGIN
            SELECT RAISE(ABORT, 'curator_entries.id is immutable');
        END
    """,
    # trigger trg_footer_entries_id_immutable
    """
CREATE TRIGGER trg_footer_entries_id_immutable
        BEFORE UPDATE OF id ON footer_entries
        FOR EACH ROW
        WHEN NEW.id <> OLD.id
        BEGIN
            SELECT RAISE(ABORT, 'footer_entries.id is immutable');
        END
    """,
    # table user_states
    """
CREATE TABLE user_states (
    user_id INTEGER NOT NULL,
    chat_id INTEGER,
    screen_message_id INTEGER,
    payload TEXT,
    updated_at DATETIME,
    PRIMARY KEY (user_id)
)
    """,
)

# Каталог системных кирпичиков новой базы: id, code, title, type, source,
# rules_json, description. Источник истины по правилам типов — код продукта
# (`brick_types.SYSTEM_BRICKS`); здесь — начальное состояние релиза 1.0.0 с
# ФИНАЛЬНЫМИ описаниями (их показывает экран вопроса).
#
# id заданы явно: номера системных кирпичиков стабильны (на них ссылаются
# шаблоны и снимки), id=6 не переиспользуется — это удалённый `адрес_места`.
_BASELINE_SYSTEM_BRICKS: tuple[
    tuple[int, str, str, str, str, str | None, str | None], ...
] = (
    (
        1,
        "название",
        "Название мероприятия",
        "short_text",
        "ask",
        None,
        "Напишите название мероприятия.",
    ),
    (
        2,
        "дата",
        "Дата",
        "date",
        "ask",
        '{"horizon_months": 12}',
        "Выберите дату мероприятия или напишите её.",
    ),
    (
        3,
        "время_начала",
        "Время начала",
        "time",
        "ask",
        None,
        "Выберите время начала мероприятия или напишите.",
    ),
    (4, "время_окончания", "Время окончания", "time", "ask", None, None),
    (5, "место", "Название места", "short_text", "place", None, None),
    (7, "ссылка_места", "Ссылка места", "url", "place", None, None),
    (
        8,
        "описание",
        "Описание",
        "long_text",
        "ask",
        None,
        "Пришлите описание мероприятия.",
    ),
    (
        9,
        "контакт",
        "Контакт",
        "short_text",
        "ask",
        None,
        "Напишите контакт для гостей по вопросам мероприятия. Можно указать @ник, t.me/ник или имя с гиперссылкой.",
    ),
    (10, "адрес", "Адрес", "short_text", "place", None, None),
    (11, "город", "Город", "short_text", "place", None, None),
    (
        12,
        "условие_входа",
        "Условие входа",
        "short_text",
        "ask",
        None,
        "Напишите условия входа. Например: «Вход бесплатный, без регистрации», «Вход — 100 ₽» или «Вход по записи».",
    ),
    (
        13,
        "куратор",
        "Куратор",
        "short_text",
        "ask",
        None,
        "Выберите куратора или пришлите его контакт: @ник, ссылку или текст с гиперссылкой.",
    ),
    (
        14,
        "подвал",
        "Подвал",
        "short_text",
        "ask",
        None,
        "Выберите напоминание, которое появится внизу анонса.",
    ),
)


def _sql_literal(value: "str | None") -> str:
    """Литерал SQL: NULL или строка в кавычках (одинарная кавычка удваивается)."""
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def _baseline_system_brick_insert(
    brick_id: int,
    code: str,
    title: str,
    value_type: str,
    source: str,
    rules_json: "str | None",
    description: "str | None",
) -> str:
    """
    Идемпотентная вставка одного системного кирпичика.

    `WHERE NOT EXISTS` по коду и области: повторная инициализация не создаёт
    дублей и НЕ перезаписывает описания, отредактированные владельцем — та же
    семантика «не откатывать правку», что у условных UPDATE прежних версий.
    """
    code_sql = _sql_literal(code)
    values = ", ".join(
        (
            str(brick_id),
            code_sql,
            _sql_literal(title),
            _sql_literal(value_type),
            "'system'",
            _sql_literal(source),
            _sql_literal(rules_json),
            "'active'",
            "NULL",
            "CURRENT_TIMESTAMP",
            "CURRENT_TIMESTAMP",
            _sql_literal(description),
        )
    )
    return (
        """
        INSERT INTO bricks
            (id, code, title, type, scope, source, rules_json, status,
             created_by_user_id, created_at, updated_at, description)
        SELECT """
        + values
        + """
        FROM (SELECT 1)
        WHERE NOT EXISTS (
            SELECT 1 FROM bricks
            WHERE code = """
        + code_sql
        + """ AND scope = 'system'
        )
        """
    )


_BASELINE_SEED: tuple[str, ...] = tuple(
    _baseline_system_brick_insert(*brick) for brick in _BASELINE_SYSTEM_BRICKS
)

# Базовая версия 1.0.0: пустой файл → полностью готовая схема и каталог
# системных кирпичиков. Контрольная сумма считается по всем шагам (DDL +
# сидирование): применённая версия неизменяема, правки каталога — новая
# версия 2, 3, …
MIGRATION_1_0_0_BASELINE = Migration(
    version=1,
    name="baseline_1_0_0",
    # Отступы блоков в исходнике не часть DDL: `strip` возвращает текст
    # оператора к эталонному виду (SQLite сохраняет его в sqlite_master как
    # есть, поэтому текст совпадает с эталоном).
    statements=tuple(statement.strip() for statement in _BASELINE_SCHEMA)
    + _BASELINE_SEED,
)

DEFAULT_MIGRATIONS: tuple[Migration, ...] = (MIGRATION_1_0_0_BASELINE,)

LATEST_VERSION = DEFAULT_MIGRATIONS[-1].version if DEFAULT_MIGRATIONS else 0


def _sqlite_file_path(engine: Engine) -> str:
    """Путь к файлу SQLite; миграции не поддерживают анонимные базы."""
    if engine.url.get_backend_name() != "sqlite":
        raise MigrationError(
            "Миграции Poster поддерживают только SQLite, "
            f"а не {engine.url.get_backend_name()!r}."
        )
    database = engine.url.database
    if not database or database == ":memory:":
        raise MigrationError(
            "Миграции требуют файловую SQLite-базу: путь к файлу не задан "
            "(DATABASE_URL указывает на :memory:)."
        )
    return str(database)


def _validate_migrations(migrations: Sequence[Migration]) -> None:
    """Версии обязаны быть непрерывными, начиная с 1 (без пропусков)."""
    versions = [migration.version for migration in migrations]
    expected = list(range(1, len(versions) + 1))
    if versions != expected:
        raise ValueError(
            "Список миграций должен содержать непрерывные версии 1..N; "
            f"получено {versions!r}."
        )
    for migration in migrations:
        if not migration.name.strip():
            raise ValueError(f"У миграции {migration.version} пустое имя.")
        if not migration.statements:
            raise ValueError(f"У миграции {migration.version} нет шагов.")


def _validate_journal(
    applied: Sequence[sqlite3.Row], migrations: Sequence[Migration]
) -> None:
    """
    Журнал должен быть префиксом списка миграций кода с теми же суммами.

    Проверяются: версии (без чужих/пропущенных) и контрольные суммы DDL.
    """
    applied_versions = [int(row["version"]) for row in applied]
    expected_prefix = [migration.version for migration in migrations[: len(applied)]]
    if applied_versions != expected_prefix:
        known = {migration.version for migration in migrations}
        unknown = [version for version in applied_versions if version not in known]
        if unknown:
            raise IncompatibleSchemaError(
                "База содержит миграции, неизвестные этому коду: "
                f"{unknown!r}. Вероятно, база создана более новой версией "
                "Poster; обновите приложение или восстановите резервную копию."
            )
        raise IncompatibleSchemaError(
            "Журнал миграций не является непрерывным: "
            f"применено {applied_versions!r}, ожидался префикс "
            f"{expected_prefix!r}."
        )

    for row, migration in zip(applied, migrations):
        if str(row["checksum"]) != migration.checksum:
            raise IncompatibleSchemaError(
                f"Миграция {migration.version} ({migration.name}) была "
                "изменена после применения: контрольная сумма DDL не "
                "совпадает. Применяйте только добавление новых миграций."
            )


def _open_source_connection(path: str) -> sqlite3.Connection:
    """
    Соединение с исходной базой для чтения/backup.

    Сначала пробуем строго read-only URI: backup не должен менять рабочую
    базу. Для WAL-базы после аварийного завершения может отсутствовать
    `-shm`; в этом случае SQLite иногда не открывает файл только для чтения.
    Тогда используется обычное соединение (оно не меняет пользовательские
    данные, но позволяет SQLite корректно прочитать WAL).
    """
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return connection
    except sqlite3.Error:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
    return sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)


def _has_pending_migrations(path: str, migrations: Sequence[Migration]) -> bool:
    """
    Есть ли что применять — без записи в базу.

    Новый/пустой файл ещё не содержит пользовательских данных: копия не
    нужна. Для существующей базы читаем только журнал; его отсутствие на
    старой базе означает, что ожидают все миграции.
    """
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return False
        connection = _open_source_connection(path)
        try:
            journal_exists = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if journal_exists is None:
                return bool(migrations)
            row = connection.execute(
                "SELECT count(*) FROM schema_migrations"
            ).fetchone()
            applied = int(row[0]) if row is not None else 0
            return applied < len(migrations)
        finally:
            connection.close()
    except (sqlite3.Error, OSError) as exc:
        raise BackupError(
            "Не удалось прочитать состояние базы перед миграцией, поэтому "
            f"безопасная резервная копия невозможна: {exc}"
        ) from exc


def _backup_destination(path: str) -> Path:
    """Уникальное имя рядом с базой; существующие файлы не перезаписываются."""
    base = Path(path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = base.with_name(f"{base.name}{BACKUP_SUFFIX}{stamp}-p{os.getpid()}")
    counter = 1
    while candidate.exists():
        candidate = base.with_name(
            f"{base.name}{BACKUP_SUFFIX}{stamp}-p{os.getpid()}-{counter}"
        )
        counter += 1
    return candidate


def create_database_backup(path: str) -> Path:
    """
    Создаёт согласованную копию SQLite через Backup API.

    Прямое копирование только основного файла при WAL потеряло бы последние
    зафиксированные транзакции. `Connection.backup()` читает базу как SQLite,
    включая WAL, и создаёт цельный снимок. Копия сначала пишется во временный
    файл, проверяется `PRAGMA integrity_check` и только затем атомарно
    переименовывается. Готовые копии никогда не удаляются.
    """
    source_path = Path(path)
    temp_name: str | None = None
    source: sqlite3.Connection | None = None
    target: sqlite3.Connection | None = None
    try:
        if not source_path.exists() or source_path.stat().st_size == 0:
            raise BackupError(f"Нет базы для копирования: {path}")
        destination = _backup_destination(path)
        fd, temp_name = tempfile.mkstemp(
            prefix=f"{source_path.name}.backup-tmp-", dir=str(source_path.parent)
        )
        os.close(fd)
        # Не удаляем файл: mkstemp сразу создал его с правами 0600. SQLite
        # открывает пустой файл как пустую базу и пишет копию уже без окна,
        # в котором файл мог бы получить более широкие права.
        source = _open_source_connection(path)
        target = sqlite3.connect(
            temp_name,
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        source.backup(target)
        row = target.execute("PRAGMA integrity_check").fetchone()
        if row is None or str(row[0]).lower() != "ok":
            raise BackupError("Резервная копия не прошла проверку целостности SQLite.")
        target.close()
        target = None
        source.close()
        source = None
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, destination)
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(
            f"Не удалось создать резервную копию перед миграцией: {exc}"
        ) from exc
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
        if temp_name is not None and os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                logger.warning("Не удалось удалить временный файл копии %s", temp_name)
    logger.info("Создана резервная копия базы перед миграцией: %s", destination)
    return destination


def _missing_tables(connection: sqlite3.Connection, names: Sequence[str]) -> list[str]:
    """Каких из ожидаемых таблиц нет в базе (пустой список — все на месте)."""
    missing: list[str] = []
    for name in names:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (name,),
        ).fetchone()
        if row is None:
            missing.append(name)
    return missing


def run_migrations(
    engine: Engine,
    migrations: Sequence[Migration] = DEFAULT_MIGRATIONS,
) -> int:
    """
    Применяет недостающие миграции и возвращает последнюю версию схемы.

    `BEGIN IMMEDIATE` берётся до чтения журнала: конкурентный запуск
    (например, два старта бота) ждёт освобождения блокировки и затем видит
    уже применённые версии. Все ожидающие миграции идут одной транзакцией:
    сбой любой из них откатывает весь запуск, журнал не пополняется.

    Исключения:
    - IncompatibleSchemaError — журнал/схема не совместимы с кодом;
    - MigrationError — не удалось применить DDL (база не изменена).
    """
    _validate_migrations(migrations)
    path = _sqlite_file_path(engine)

    # Защита пользовательских данных: копия создаётся ДО первой блокировки
    # и любой DDL. Если копию сделать нельзя, миграция не начинается.
    if _has_pending_migrations(path, migrations):
        create_database_backup(path)

    connection = sqlite3.connect(
        path,
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,  # транзакциями управляем явно
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise MigrationError(
                "Не удалось получить блокировку базы для миграций за "
                f"{BUSY_TIMEOUT_MS // 1000} секунд: {exc}"
            ) from exc

        try:
            connection.execute(_JOURNAL_DDL)
            applied = connection.execute(
                "SELECT version, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
            _validate_journal(applied, migrations)

            for migration in migrations[len(applied) :]:
                logger.info(
                    "Применяется миграция %s (%s).",
                    migration.version,
                    migration.name,
                )
                missing = _missing_tables(connection, migration.requires_tables)
                if missing:
                    # Таблицы нет — шаги неприменимы. Версия фиксируется:
                    # повторный запуск не должен пытаться снова.
                    logger.info(
                        "Миграция %s (%s) пропущена: нет таблиц %s.",
                        migration.version,
                        migration.name,
                        ", ".join(missing),
                    )
                else:
                    try:
                        for statement in migration.statements:
                            connection.execute(statement)
                    except Exception as exc:
                        # Любая ошибка шага = миграция не применена;
                        # внешний обработчик откатит ВСЮ транзакцию.
                        raise MigrationError(
                            f"Миграция {migration.version} ({migration.name}) "
                            f"не применена: {exc}"
                        ) from exc
                connection.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) "
                    "VALUES (?, ?, ?)",
                    (migration.version, migration.name, migration.checksum),
                )

            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()

    return migrations[-1].version if migrations else 0
