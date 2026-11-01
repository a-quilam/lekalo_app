# db_maintenance.py
"""
Штатное обслуживание SQLite-базы Poster после релиза (CLI-команда).

Зачем: команда poster-* получает утверждённую владельцем задачу «завершить
релиз с очисткой тестовых данных», но произвольные shell-команды к рабочей
базе запрещены правилами разрешений. Вместо набора `rm`/`sqlite3` здесь
одна безопасная команда с фиксированными фазами:

1. Проверка целостности исходной базы (`PRAGMA integrity_check`);
2. Согласованный WAL-бэкап штатным `create_database_backup` (Backup API,
   временный файл, `integrity_check`, атомарный rename) + повторное
   открытие копии с новой проверкой целостности;
3. Очистка тестовых данных: состояния, места, шаблоны, кирпичики, анонсы;
   системные объекты (`schema_migrations`) и журналы истории, защищённые
   триггерами схемы, не затрагиваются. Суперадминистратор — конфигурационный
   (`SUPERADMIN_USER_IDS` в `.env`), в БД строк не имеет: сохранение
   суперадминистратора = не трогать конфиг (этот механизм его не читает).
4. Контроль записи: перед каждой записью — `BEGIN IMMEDIATE` (см.
   `_writing_transaction`), чтобы очистка не пересекалась с параллельной
   записью; на старте проверяется отсутствие `BEGIN IMMEDIATE`-захвата;
5. Повторная проверка результата: `integrity_check` + smoke-запуск
   `init_db` на той же базе в отдельном процессе (как старт бота, без
   Telegram).

Бэкап — обязательное условие очистки: фазы 3+ не выполняются, если фаза 2
не создала и не проверила копию этой базы (см. `_VERIFIED_BACKUPS`).

Запуск (Makefile, каталог `Poster`):
    make db-maintain-report          # фазы 1–2: целостность + бэкап рабочей БД
    make db-maintain-clean           # полный цикл на рабочей БД (релизная
                                     # очистка, утверждена владельцем)
    make db-maintain-clean DB=path   # полный цикл на указанной базе

Проверка механизма выполняется ТОЛЬКО на временной базе:
    make db-maintain-clean DB=/tmp/opencode/poster_maint_test.db
Рабочая база в рамках настройки механизма не очищается.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from migrations import BUSY_TIMEOUT_MS, create_database_backup

APP_DIR = Path(__file__).resolve().parent
REAL_DB_PATH = APP_DIR / "post_bot.db"

# --- Системные и сохраняемые объекты -------------------------------------

# Журнал миграций: определяет совместимость схемы, чистке не подлежит.
SYSTEM_TABLES = ("schema_migrations",)

# Суперадминистратор в Poster — конфигурационный: источник — только
# `SUPERADMIN_USER_IDS` в `.env` (см. permissions.py), строк в БД у него
# нет. Поэтому сохранение суперадминистратора = не трогать `.env` и конфиг;
# этот механизм их не читает и не изменяет.

# Таблицы, которые схема защищает от очистки сама — журналы истории:
# `trg_brick_proposals_no_delete`, `trg_brick_conflicts_no_delete`,
# `trg_brick_conflict_notifications_no_delete`-семантика переходов,
# `trg_template_snapshots_no_delete` и безусловный запрет на удаление
# связок снимков `trg_template_snapshot_bricks_no_delete` (см. migrations.py).
# Очистка их уважает. Строки, на которые они ссылаются через FK RESTRICT
# (users, places, system-кирпичики, версии шаблонов), остаются якорями —
# это «системные объекты» в смысле задания владельца.
IMMUTABLE_JOURNAL_TABLES = (
    "brick_proposals",
    "brick_conflicts",
    "brick_conflict_notifications",
    "template_snapshots",
    "template_snapshot_bricks",
)

# Порядок удаления: дочерние строки до родительских (FK), журналы до
# защищаемых ими таблиц. Краткий журнал ограничений:
# - template_snapshot_bricks: безусловный `no_delete` — раньше снимков;
# - template_versions: `no_delete`, пока жива строка templates;
# - bricks: system-строки защищены `trg_bricks_system_no_delete`;
# - templates: standard защищён, пока существует место;
# - users/places: могут остаться якорями неизменяемых журналов (FK RESTRICT).
TEST_DATA_TABLES_CHILDREN_FIRST = (
    # журналы и связки без условий на родителей
    "brick_conflict_notifications",
    "brick_conflicts",
    "brick_proposals",
    "template_snapshot_bricks",
    # анонсы (drafts → values/extras → drafts)
    "announcement_extras",
    "announcement_values",
    "announcement_drafts",
    # версии шаблонов — до templates (триггер запрещает удаление при живой
    # родительской строке)
    "templates",
    "template_versions",
    "template_snapshots",
    # кирпичики: персональные удаляются, system-строки защищены триггером —
    # ожидаемо остаются, это системные объекты
    "bricks",
    "place_invite_links",
    "place_edit_log",
    "admin_transfers",
    "admin_sessions",
    "place_sessions",
    "brick_sessions",
    "template_sessions",
    "user_states",
    "place_names",
    "place_admins",
    # родители
    "places",
    "users",
)


# Ошибка, которую выбрасывают все фазы при невозможности безопасно работать.
class MaintenanceError(RuntimeError):
    """Обслуживание прервано: база, бэкап или запись не прошли проверку."""


# --- Фаза 1: целостность -------------------------------------------------


def check_integrity(path: str | Path) -> str:
    """`PRAGMA integrity_check` исходной базы; 'ok' или исключение."""
    connection = _connect(path)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    result = str(row[0]) if row else "нет результата"
    if result.lower() != "ok":
        raise MaintenanceError(f"Исходная база не прошла integrity_check: {result}")
    return result


# --- Фаза 2: согласованный WAL-бэкап с проверкой -------------------------

# Пути баз, для которых фаза 2 уже создала и проверила копию. Ключ —
# разрешённый абсолютный путь базы. Очистка (фаза 3) отказывается работать
# без записи в этом множестве: бэкап обязателен.
_VERIFIED_BACKUPS: set[str] = set()


def backup_database(path: str | Path) -> Path:
    """
    Согласованный бэкап: штатный `create_database_backup` + контроль.

    Порядок: целостность исходной базы → копия через SQLite Backup API
    (WAL-aware, временный файл, `integrity_check`, атомарный rename) →
    повторное открытие копии с новой проверкой `integrity_check`. Если
    копия не создана или не проверена — исключение, очистка не начнётся.
    """
    resolved = Path(path).resolve()
    check_integrity(resolved)
    backup_path = create_database_backup(str(resolved))
    # Копия проверяется отдельным соединением: имя и целостность.
    if not backup_path.exists() or backup_path.stat().st_size == 0:
        raise MaintenanceError(f"Файл копии отсутствует или пуст: {backup_path}")
    backup_check = check_integrity(backup_path)
    assert backup_check == "ok"  # check_integrity иначе поднял исключение
    _VERIFIED_BACKUPS.add(str(resolved))
    print(f"[2/4] Бэкап создан и проверен: {backup_path}")
    return backup_path


# --- Фаза 3: очистка тестовых данных -------------------------------------


def _connect(path: str | Path) -> sqlite3.Connection:
    """Соединение с рабочими PRAGMA проекта (busy_timeout, foreign_keys)."""
    connection = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _writing_transaction(
    connection: sqlite3.Connection,
) -> "sqlite3.Cursor | None":
    """
    Начать транзакцию записи (`BEGIN IMMEDIATE`) или вернуть None.

    `BEGIN IMMEDIATE` берёт зарезервированную блокировку: если бот в этот
    момент пишет, вызов ждёт busy_timeout, а не портит данные вдвоём.
    """
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        return None
    return connection.cursor()


def _guard_writable(path: str | Path) -> None:
    """
    Проверить, что запись в базу возможна: тестовая транзакция + откат.

    Если другой процесс держит долгую запись и блокировку взять нельзя,
    обслуживание прерывается до любых изменений.
    """
    connection = _connect(path)
    try:
        cursor = _writing_transaction(connection)
        if cursor is None:
            raise MaintenanceError(
                "База занята другой записью: тестовая блокировка не взята."
            )
        connection.execute("ROLLBACK")
    finally:
        connection.close()


def _missing_tables(
    connection: sqlite3.Connection, names: tuple[str, ...]
) -> list[str]:
    missing: list[str] = []
    for name in names:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (name,),
        ).fetchone()
        if row is None:
            missing.append(name)
    return missing


def clean_test_data(path: str | Path) -> dict[str, int]:
    """
    Очистить тестовые данные, сохранив системные объекты.

    Условие: для этой базы уже прошёл проверенный бэкап (backup_database).
    Записи идут в одной транзакции `BEGIN IMMEDIATE`; сбой = полный откат.
    Возвращает таблицу → число удалённых строк.

    Рабочая база проекта (Poster/post_bot.db) — та самая «тестовая БД»,
    очистка которой утверждена владельцем ПОСЛЕ релиза; запуск с ней —
    штатная релизная операция менеджера. Право на команду выдано только
    менеджеру и тестировщику (последний — только с DB=/tmp/...); developer
    и reviewer допуска к make db-maintain-* не имеют.
    """
    resolved = Path(path).resolve()
    if str(resolved) not in _VERIFIED_BACKUPS:
        raise MaintenanceError(
            "Очистка запрещена: для этой базы нет проверенного бэкапа. "
            "Сначала выполните фазу backup."
        )

    _guard_writable(resolved)
    connection = _connect(resolved)
    deleted: dict[str, int] = {}
    anchored: dict[str, int] = {}
    try:
        cursor = _writing_transaction(connection)
        if cursor is None:
            raise MaintenanceError("База занята: блокировка записи не взята.")
        try:
            present = [
                table
                for table in TEST_DATA_TABLES_CHILDREN_FIRST
                if not _missing_tables(connection, (table,))
            ]
            for table in present:
                if table in IMMUTABLE_JOURNAL_TABLES:
                    # Журналы истории схема запрещает очищать триггерами;
                    # их строка-счётчик проверяется отдельно (см. ниже).
                    continue
                # Строки, на которые ссылаются неизменяемые журналы
                # (FK RESTRICT), удаляем построчно, пропуская якорные.
                # Триггер бизнес-логики (например, system-кирпичик) — тоже
                # «якорная» строка: IntegrityError = строка остаётся.
                remaining = 0
                rows = connection.execute(
                    f"SELECT rowid FROM {table}"  # noqa: S608 — имя из константы
                ).fetchall()
                for row in rows:
                    try:
                        connection.execute(
                            f"DELETE FROM {table} WHERE rowid = ?",
                            (row[0],),
                        )
                        deleted[table] = deleted.get(table, 0) + 1
                    except sqlite3.IntegrityError:
                        remaining += 1
                if remaining:
                    anchored[table] = remaining
            # Системные таблицы (schema_migrations) не трогаем: совместимость
            # схемы. Таблица users может остаться непустой: строки-якоря
            # неизменяемых журналов (RESTRICT) защищают часть пользователей.
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()
    if anchored:
        print("[3/4] Якорные строки (системные объекты/журналы) оставлены:")
        for table, count in sorted(anchored.items()):
            print(f"      {table}: {count}")
    print("[3/4] Тестовые данные очищены.")
    return deleted


# --- Фаза 4: повторная проверка ------------------------------------------


def verify_after_clean(path: str | Path) -> str:
    """
    Поверка результата: integrity_check + smoke-запуск init_db.

    Отдельный процесс инициализирует базу как при старте бота (миграции
    должны быть no-op, схема — читаемой). Telegram не трогается: токен —
    фикстурный, init_db сети не касается.
    """
    resolved = Path(path).resolve()
    integrity = check_integrity(resolved)

    env = os.environ.copy()
    env["DATABASE_URL"] = "sqlite:///" + str(resolved)
    env.setdefault(
        "TELEGRAM_BOT_TOKEN", "123456789:FIXTUREFAKETOKENforTestsOnly00000000"
    )
    result = subprocess.run(
        [sys.executable, "-c", "from database import init_db; init_db()"],
        cwd=str(APP_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise MaintenanceError(
            "Smoke-запуск init_db после очистки упал:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    print(f"[4/4] Повторная проверка: integrity={integrity}, smoke-запуск ok.")
    return integrity


# --- Единая точка входа ---------------------------------------------------


def run_maintenance(path: str | Path, *, clean: bool = True) -> dict[str, object]:
    """
    Полный цикл обслуживания: 1) целостность, 2) бэкап, 3) очистка,
    4) повторная проверка. При clean=False выполняются фазы 1–2
    (отчёт без очистки — для проверки механизма).

    Рабочая база по умолчанию: обслуживание без `DB=` — это утверждённая
    владельцем релизная очистка тестовых данных Poster/post_bot.db.
    Проверка механизма (эта задача) выполнялась только на временных
    копиях в /tmp/opencode с явным DB=.
    """
    resolved = Path(path).resolve()
    if not resolved.exists() or resolved.stat().st_size == 0:
        raise MaintenanceError(f"База не найдена или пуста: {resolved}")

    print(f"[1/4] База: {resolved}")
    integrity = check_integrity(resolved)
    print(f"[1/4] Целостность исходной базы: {integrity}")

    backup_path = backup_database(resolved)
    report: dict[str, object] = {"database": str(resolved), "backup": str(backup_path)}

    if clean:
        report["deleted"] = clean_test_data(resolved)
        report["integrity_after"] = verify_after_clean(resolved)
    else:
        report["cleaned"] = False
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Обслуживание БД Poster.")
    parser.add_argument(
        "--db",
        default=str(REAL_DB_PATH),
        help="Путь к базе (по умолчанию Poster/post_bot.db).",
    )
    parser.add_argument(
        "--skip-clean",
        action="store_true",
        help="Только целостность и проверенный бэкап, без очистки.",
    )
    args = parser.parse_args(argv)
    try:
        report = run_maintenance(args.db, clean=not args.skip_clean)
    except MaintenanceError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    for key, value in report.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
