# tests/test_baseline_1_0_0.py
"""
Релиз 1.0.0: единая базовая инициализация схемы вместо 33 миграций.

Главный критерий — СЕМАНТИЧЕСКАЯ идентичность эталону, созданному
накопительными миграциями 1–33 (`POSTER_REF_DIR`, по умолчанию
`/tmp/opencode`). Эталон — зафиксированный слепок чистой базы релиза:

- `reference_schema.json` — все объекты `sqlite_master` (`тип|имя` → SQL);
- `ref_db_semantic.json` — колонки/типы/notnull/default/pk, FK и индексы
  каждой таблицы;
- `ref_triggers.json` — все 45 триггеров (имя → SQL);
- `ref_bricks.json` — 13 системных кирпичиков с финальными описаниями.

Допустимые различия с эталоном: форматирование SQL (пробелы, отступы,
регистр), запись журнала (одна строка вместо 33) и `created_at`/`updated_at`
кирпичиков (момент инициализации).

Проверяется на ВРЕМЕННЫХ файлах SQLite; рабочая `Poster/post_bot.db`,
`.env`, Telegram и сеть не используются.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import unittest
from pathlib import Path

from sqlalchemy.engine import Engine

import database
from base import Base
from migrations import (
    DEFAULT_MIGRATIONS,
    LATEST_VERSION,
    MIGRATION_1_0_0_BASELINE,
    IncompatibleSchemaError,
    run_migrations,
)
from tests.stage19a_helpers import connect, make_engine, temp_db_path

# Каталог слепков эталона. CI без него: тесты сравнения SKIP, а проверки
# самой инициализации (журнал, идемпотентность, совместимость) идут всегда.
REF_DIR = Path(os.environ.get("POSTER_REF_DIR", "/tmp/opencode"))
REF_SCHEMA = REF_DIR / "reference_schema.json"
REF_SEMANTIC = REF_DIR / "ref_db_semantic.json"
REF_TRIGGERS = REF_DIR / "ref_triggers.json"
REF_BRICKS = REF_DIR / "ref_bricks.json"

REF_MISSING = (
    None
    if all(
        path.exists() for path in (REF_SCHEMA, REF_SEMANTIC, REF_TRIGGERS, REF_BRICKS)
    )
    else f"слепки эталона не найдены в {REF_DIR} (POSTER_REF_DIR)"
)

BASELINE_NAME = "baseline_1_0_0"

# Системные кирпичики: 13 определений, id=6 не переиспользуется.
SYSTEM_BRICK_IDS = [1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14]


# —— помощники --------------------------------------------------------------


def normalise(sql: str | None) -> str:
    """Семантическое сравнение SQL: различия пробелов и регистра не важны."""
    return " ".join((sql or "").lower().split())


def init_new_database(path: str) -> Engine:
    """
    Ровно `init_db()`, но на переданном файле: версии схемы + create_all.

    `init_db()` работает с глобальным движком модуля; здесь нужен тот же
    порядок шагов на временном файле.
    """
    engine = make_engine(path)
    run_migrations(engine)
    Base.metadata.create_all(bind=engine)
    return engine


def sqlite_master(path: str) -> dict[str, str | None]:
    connection = connect(path)
    try:
        return {
            f"{row['type']}|{row['name']}": row["sql"]
            for row in connection.execute("SELECT type, name, sql FROM sqlite_master")
        }
    finally:
        connection.close()


def journal(path: str) -> list[tuple[int, str]]:
    connection = connect(path)
    try:
        return [
            (int(row["version"]), str(row["name"]))
            for row in connection.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            )
        ]
    finally:
        connection.close()


def scalar(path: str, sql: str, parameters: tuple = ()):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql, parameters).fetchone()
    finally:
        connection.close()


def bricks(path: str) -> list[dict]:
    connection = connect(path)
    try:
        return [
            dict(row) for row in connection.execute("SELECT * FROM bricks ORDER BY id")
        ]
    finally:
        connection.close()


class _TempDatabase(unittest.TestCase):
    def setUp(self) -> None:
        self.path = temp_db_path("poster-baseline-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)


# —— контракт базовой версии ------------------------------------------------


class BaselineContractTests(_TempDatabase):
    """Версия 1.0.0 как единственная запись журнала и как механизм."""

    def test_default_migrations_is_one_baseline(self) -> None:
        self.assertEqual(LATEST_VERSION, 1)
        self.assertEqual(len(DEFAULT_MIGRATIONS), 1)
        baseline = DEFAULT_MIGRATIONS[0]
        self.assertEqual(baseline.version, 1)
        self.assertEqual(baseline.name, BASELINE_NAME)
        self.assertIs(baseline, MIGRATION_1_0_0_BASELINE)

    def test_checksum_is_sha256_of_ddl(self) -> None:
        checksum = MIGRATION_1_0_0_BASELINE.checksum
        self.assertEqual(len(checksum), 64)
        int(checksum, 16)
        # Сумма зафиксирована в базе и защищает от правки применённой версии.
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        stored = scalar(self.path, "SELECT checksum FROM schema_migrations")
        self.assertEqual(stored, (checksum,))

    def test_empty_file_becomes_complete_schema(self) -> None:
        # Файл ещё не создан или пуст: база строится с нуля.
        self.assertFalse(os.path.exists(self.path) and os.path.getsize(self.path))

        run_migrations(self.engine)

        tables = {
            key.split("|", 1)[1]
            for key in sqlite_master(self.path)
            if key.startswith("table|")
        }
        # 32 таблицы: вся схема, включая `user_states` (раньше её создавал
        # только create_all) и журнал версий.
        self.assertEqual(len(tables), 32)
        for expected in (
            "users",
            "places",
            "bricks",
            "templates",
            "announcement_drafts",
            "curator_entries",
            "footer_entries",
            "reports",
            "user_states",
            "schema_migrations",
        ):
            with self.subTest(table=expected):
                self.assertIn(expected, tables)
        # Легаси-артефактов прежних миграций нет и в новой схеме.
        for legacy in (
            "drafts",
            "legacy_drafts_backup",
            "migration_18_targets",
            "migration_22_targets",
        ):
            with self.subTest(legacy=legacy):
                self.assertNotIn(legacy, tables)

        triggers = {
            key for key in sqlite_master(self.path) if key.startswith("trigger|")
        }
        self.assertEqual(len(triggers), 45)
        indexes = {key for key in sqlite_master(self.path) if key.startswith("index|")}
        self.assertEqual(len(indexes), 73)

        self.assertEqual(journal(self.path), [(1, BASELINE_NAME)])

    def test_journal_has_single_baseline_record(self) -> None:
        run_migrations(self.engine)
        rows = journal(self.path)
        self.assertEqual(len(rows), 1)
        version, name = rows[0]
        self.assertEqual(version, 1)
        self.assertEqual(name, BASELINE_NAME)
        # applied_at заполняется механизмом (DEFAULT CURRENT_TIMESTAMP).
        self.assertIsNotNone(
            scalar(self.path, "SELECT applied_at FROM schema_migrations")
        )

    def test_user_states_is_part_of_baseline(self) -> None:
        run_migrations(self.engine)
        info = {
            row[1]: row
            for row in connect(self.path).execute("PRAGMA table_info(user_states)")
        }
        self.assertEqual(
            set(info),
            {"user_id", "chat_id", "screen_message_id", "payload", "updated_at"},
        )
        self.assertEqual(info["user_id"][5], 1)  # PRIMARY KEY
        self.assertEqual(info["user_id"][3], 1)  # NOT NULL
        for nullable in ("chat_id", "screen_message_id", "payload", "updated_at"):
            with self.subTest(column=nullable):
                self.assertEqual(info[nullable][3], 0)

    def test_create_all_is_noop_after_baseline(self) -> None:
        run_migrations(self.engine)
        before = sqlite_master(self.path)
        Base.metadata.create_all(bind=self.engine)
        self.assertEqual(sqlite_master(self.path), before)


# —— сидирование системных кирпичиков ---------------------------------------


class BaselineSeedTests(_TempDatabase):
    """13 системных кирпичиков с финальными описаниями, идемпотентно."""

    def test_seed_creates_thirteen_system_bricks(self) -> None:
        run_migrations(self.engine)
        rows = bricks(self.path)
        self.assertEqual(len(rows), 13)
        self.assertEqual([row["id"] for row in rows], SYSTEM_BRICK_IDS)
        self.assertNotIn(6, [row["id"] for row in rows])
        for row in rows:
            with self.subTest(code=row["code"]):
                self.assertEqual(row["scope"], "system")
                self.assertEqual(row["status"], "active")
                self.assertIsNone(row["place_id"])
                self.assertIsNone(row["owner_user_id"])
                self.assertIsNone(row["created_by_user_id"])
                self.assertEqual(row["revision"], 1)

    def test_final_descriptions_are_seeded(self) -> None:
        run_migrations(self.engine)
        descriptions = {row["code"]: row["description"] for row in bricks(self.path)}
        self.assertEqual(
            descriptions,
            {
                "название": "Напишите название мероприятия.",
                "дата": "Выберите дату мероприятия или напишите её.",
                "время_начала": "Выберите время начала мероприятия или напишите.",
                "время_окончания": None,
                "место": None,
                "ссылка_места": None,
                "описание": "Пришлите описание мероприятия.",
                "контакт": (
                    "Напишите контакт для гостей по вопросам мероприятия. "
                    "Можно указать @ник, t.me/ник или имя с гиперссылкой."
                ),
                "адрес": None,
                "город": None,
                "условие_входа": (
                    "Напишите условия входа. Например: «Вход бесплатный, "
                    "без регистрации», «Вход — 100 ₽» или «Вход по записи»."
                ),
                "куратор": (
                    "Выберите куратора или пришлите его контакт: @ник, "
                    "ссылку или текст с гиперссылкой."
                ),
                "подвал": "Выберите напоминание, которое появится внизу анонса.",
            },
        )

    def test_repeated_init_does_not_duplicate_or_rewrite(self) -> None:
        run_migrations(self.engine)
        before = bricks(self.path)
        for _ in range(2):
            run_migrations(self.engine)
            Base.metadata.create_all(bind=self.engine)
        after = bricks(self.path)
        self.assertEqual(len(after), 13)
        self.assertEqual(after, before)
        self.assertEqual(len(journal(self.path)), 1)

    def test_owner_edit_survives_reinit(self) -> None:
        """Правка владельца описания не откатывается повторной инициализацией."""
        run_migrations(self.engine)
        connection = connect(self.path)
        try:
            connection.execute(
                "UPDATE bricks SET description = 'Мой текст', "
                "updated_at = CURRENT_TIMESTAMP WHERE code = 'контакт'"
            )
            connection.commit()
        finally:
            connection.close()

        run_migrations(self.engine)
        Base.metadata.create_all(bind=self.engine)

        stored = {
            row["code"]: row["description"]
            for row in bricks(self.path)
            if row["code"] == "контакт"
        }
        self.assertEqual(stored, {"контакт": "Мой текст"})
        self.assertEqual(len(bricks(self.path)), 13)

    def test_seed_step_is_idempotent(self) -> None:
        """Повторное выполнение шагов сидирования ничего не меняет."""
        run_migrations(self.engine)
        before = bricks(self.path)
        from migrations import _BASELINE_SEED  # noqa: PLC0415 — внутренний шаг

        connection = sqlite3.connect(self.path)
        try:
            for statement in _BASELINE_SEED:
                connection.execute(statement)
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(bricks(self.path), before)
        self.assertEqual(len(bricks(self.path)), 13)


# —— сохранность данных на повторной инициализации --------------------------


class BaselineDataSafetyTests(_TempDatabase):
    def test_data_survives_reinit(self) -> None:
        run_migrations(self.engine)
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO users (id, telegram_user_id, created_at) "
                "VALUES (1, 777001, CURRENT_TIMESTAMP)"
            )
            connection.exec_driver_sql(
                "INSERT INTO places (id, created_by_user_id, created_at, address) "
                "VALUES (1, 1, CURRENT_TIMESTAMP, 'ул. Тестовая, 1')"
            )
            connection.exec_driver_sql(
                "INSERT INTO place_names (id, place_id, name, name_search, "
                "is_canonical, created_by_user_id, created_at) VALUES "
                "(1, 1, 'Тестовый зал', 'тестовый зал', 1, 1, CURRENT_TIMESTAMP)"
            )
            connection.exec_driver_sql(
                "INSERT INTO user_states (user_id, chat_id, payload) "
                "VALUES (777001, 555, '{\"screen\": 1}')"
            )

        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        Base.metadata.create_all(bind=self.engine)

        self.assertEqual(
            scalar(self.path, "SELECT name FROM place_names WHERE place_id = 1"),
            ("Тестовый зал",),
        )
        self.assertEqual(
            scalar(self.path, "SELECT address FROM places WHERE id = 1"),
            ("ул. Тестовая, 1",),
        )
        self.assertEqual(
            scalar(self.path, "SELECT payload FROM user_states WHERE user_id = 777001"),
            ('{"screen": 1}',),
        )
        self.assertEqual(scalar(self.path, "SELECT count(*) FROM bricks"), (13,))
        self.assertEqual(len(journal(self.path)), 1)


# —— несовместимость --------------------------------------------------------


class BaselineIncompatibilityTests(_TempDatabase):
    def _insert_journal(
        self, version: int, name: str, checksum: str = "unknown"
    ) -> None:
        run_migrations(self.engine)
        connection = connect(self.path)
        try:
            connection.execute(
                "INSERT INTO schema_migrations (version, name, checksum) "
                "VALUES (?, ?, ?)",
                (version, name, checksum),
            )
            connection.commit()
        finally:
            connection.close()

    def test_future_version_is_incompatible(self) -> None:
        """Журнал с версией 2 (будущая схема) отвергается, данные целы."""
        self._insert_journal(2, "future_schema")
        with self.assertRaises(IncompatibleSchemaError) as caught:
            run_migrations(self.engine)
        self.assertIn("2", str(caught.exception))
        self.assertEqual(scalar(self.path, "SELECT count(*) FROM bricks"), (13,))

    def test_gap_in_journal_is_incompatible(self) -> None:
        """Разрыв журнала (версия без предшествующих) — тоже несовместимость."""
        self._insert_journal(3, "future_schema")
        with self.assertRaises(IncompatibleSchemaError) as caught:
            run_migrations(self.engine)
        # Вершина кода — 1, поэтому версия 3 не только разрыв, но и чужая.
        self.assertIn("3", str(caught.exception))
        self.assertIn("неизвестн", str(caught.exception).lower())

    def test_edited_baseline_is_incompatible(self) -> None:
        """Подменённая контрольная сумма базовой версии = изменённый DDL."""
        run_migrations(self.engine)
        connection = connect(self.path)
        try:
            connection.execute("UPDATE schema_migrations SET checksum = 'deadbeef'")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(IncompatibleSchemaError) as caught:
            run_migrations(self.engine)
        self.assertIn("контрольная сумма", str(caught.exception).lower())

    def test_backup_created_before_pending_ddl(self) -> None:
        """Непустая база без журнала получает WAL-aware копию перед DDL."""
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "CREATE TABLE legacy_marker (id INTEGER PRIMARY KEY, note TEXT)"
            )
            connection.execute("INSERT INTO legacy_marker (note) VALUES ('было')")
            connection.commit()
        finally:
            connection.close()

        run_migrations(self.engine)

        copies = sorted(Path(self.path).parent.glob(Path(self.path).name + ".backup-*"))
        self.assertEqual(len(copies), 1, copies)
        self.assertEqual(
            scalar(str(copies[0]), "SELECT note FROM legacy_marker"), ("было",)
        )
        self.assertEqual(journal(self.path), [(1, BASELINE_NAME)])

    def test_real_database_is_not_touched(self) -> None:
        """Тесты инициализируют только временный файл, не рабочую базу."""
        real = Path(database.BASE_DIR) / "post_bot.db"
        # `tests/__init__.py` перенаправляет DATABASE_URL в одноразовый файл.
        # Проверяется именно настройка окружения, а не движок, созданный при
        # импорте `database`: при `unittest discover` модуль импортируется
        # раньше пакета `tests`, поэтому движок может помнить путь по
        # умолчанию, хотя ни один тест его не использует.
        configured = os.environ.get("DATABASE_URL") or database.DEFAULT_SQLITE_URL
        configured_path = configured.removeprefix("sqlite:///")
        self.assertNotEqual(
            os.path.realpath(configured_path),
            os.path.realpath(real),
            "тесты настроены на рабочую базу",
        )


# —— сравнение с эталоном релиза -------------------------------------------


@unittest.skipIf(REF_MISSING is not None, f"эталон недоступен: {REF_MISSING}")
class BaselineReferenceComparisonTests(_TempDatabase):
    """Семантическое сравнение новой базы со слепком чистой базы релиза."""

    def setUp(self) -> None:
        super().setUp()
        run_migrations(self.engine)
        Base.metadata.create_all(bind=self.engine)

    def test_sqlite_master_matches_reference(self) -> None:
        reference = json.loads(REF_SCHEMA.read_text(encoding="utf-8"))
        actual = sqlite_master(self.path)
        self.assertEqual(len(reference), 150)
        self.assertEqual(set(actual), set(reference))
        for key, expected_sql in reference.items():
            with self.subTest(object=key):
                # SQLite хранит текст оператора; сравниваем без учёта
                # пробелов/регистра — ровно то, что разрешено контрактом.
                self.assertEqual(normalise(actual[key]), normalise(expected_sql))

        tables = {key for key in actual if key.startswith("table|")}
        indexes = {key for key in actual if key.startswith("index|")}
        triggers = {key for key in actual if key.startswith("trigger|")}
        self.assertEqual((len(tables), len(indexes), len(triggers)), (32, 73, 45))

    def test_columns_foreign_keys_and_indexes_match_reference(self) -> None:
        reference = json.loads(REF_SEMANTIC.read_text(encoding="utf-8"))
        connection = connect(self.path)
        try:
            for table, expected in reference.items():
                with self.subTest(table=table):
                    columns = {
                        row["name"]: [
                            row["type"].upper(),
                            int(row["notnull"]),
                            row["dflt_value"],
                            int(row["pk"]),
                        ]
                        for row in connection.execute(f'PRAGMA table_info("{table}")')
                    }
                    self.assertEqual(
                        columns,
                        {
                            name: [sql_type.upper(), int(notnull), default, int(pk)]
                            for name, (sql_type, notnull, default, pk) in expected[
                                "columns"
                            ].items()
                        },
                    )
                    fk = sorted(
                        [row["table"], row["from"], row["to"]]
                        for row in connection.execute(
                            f'PRAGMA foreign_key_list("{table}")'
                        )
                    )
                    self.assertEqual(
                        fk,
                        sorted(
                            [table_name, column, target]
                            for column, table_name, target in expected["fk"]
                        ),
                    )
                    indexes = {}
                    for row in connection.execute(f'PRAGMA index_list("{table}")'):
                        # Включая `sqlite_autoindex` составных PK: эталон
                        # учитывает их как индексы таблицы.
                        name = row["name"]
                        columns_in_index = [
                            item["name"]
                            for item in connection.execute(
                                f'PRAGMA index_info("{name}")'
                            )
                        ]
                        indexes[name] = [
                            columns_in_index,
                            bool(row["unique"]),
                        ]
                    self.assertEqual(
                        indexes,
                        {
                            name: [list(columns_in_index), bool(unique)]
                            for name, (columns_in_index, unique) in expected[
                                "indexes"
                            ].items()
                        },
                    )
        finally:
            connection.close()

    def test_triggers_match_reference(self) -> None:
        reference = json.loads(REF_TRIGGERS.read_text(encoding="utf-8"))
        actual = {
            key.split("|", 1)[1]: sql
            for key, sql in sqlite_master(self.path).items()
            if key.startswith("trigger|")
        }
        self.assertEqual(len(reference), 45)
        self.assertEqual(len(actual), 45)
        for item in reference:
            with self.subTest(trigger=item["name"]):
                self.assertIn(item["name"], actual)
                self.assertEqual(actual[item["name"]], item["sql"])

    def test_system_bricks_match_reference(self) -> None:
        reference = json.loads(REF_BRICKS.read_text(encoding="utf-8"))
        actual = bricks(self.path)
        self.assertEqual(len(reference), 13)
        self.assertEqual(len(actual), 13)
        ignored = {"created_at", "updated_at"}
        for expected, got in zip(reference, actual):
            with self.subTest(brick=expected["code"]):
                for field, value in expected.items():
                    if field in ignored:
                        continue
                    self.assertEqual(got[field], value, field)
                # Время инициализации заполняется механизмом, а не эталоном.
                self.assertIsNotNone(got["created_at"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
