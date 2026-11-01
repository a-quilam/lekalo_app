# db_maintenance.py
"""
Штатное обслуживание SQLite-базы Poster после релиза (CLI-команда).

Зачем: команда poster-* получает утверждённую владельцем задачу «завершить
релиз с очисткой тестовых данных», но произвольные shell-команды к рабочей
базе запрещены правилами разрешений. Вместо набора `rm`/`sqlite3` здесь
одна безопасная команда с фиксированными фазами.

Утверждённый контракт владельца (после каждого релиза до первого
публичного выпуска Poster): в базе не остаётся НИ ОДНОЙ тестовой записи —
площадки, пользователи, администраторы, личные и локальные кирпичики,
пользовательские шаблоны, предложения, анонсы, состояния. Сохраняются
только: актуальная схема (все миграции), системные кирпичики (каталог
`brick_types.SYSTEM_BRICKS`), встроенный стандартный шаблон (эталон
`template_types.BUILTIN_TEMPLATE`; строка появляется вместе с первым
местом), системные настройки (в БД их нет — конфигурация `.env`) и права
суперадминистратора (конфигурационные, `SUPERADMIN_USER_IDS` в `.env`).

Архитектура очистки — «бэкап → bootstrap → верификация → атомарная
замена» (rebuild), а не построчное вычёсывание:

- прежний построчный DELETE принципиально не мог достичь контракта:
  строки-якоря неизменяемых журналов (`brick_proposals`,
  `template_snapshots` и др.) защищены триггерами `no_delete` и FK
  RESTRICT, поэтому пользователи/места/шаблоны/кирпичики «оставались»;
- rebuild устраняет саму проблему: новая база создаётся штатным
  bootstrap (`init_db`: все миграции + `create_all` + сидирование
  системных кирпичиков) в НОВОМ файле, где тестовых записей нет по
  построению; защита FK (`PRAGMA foreign_keys=ON`) и триггеров не
  отключается ни на секунду;
- результат проверяется ДО замены: integrity_check, полный журнал
  миграций 1..N, каталог системных кирпичиков по кодам из кода продукта,
  нулевые счётчики всех пользовательских таблиц; при любой неудаче
  рабочая база не изменяется;
- замена атомарна (`os.replace` в одном каталоге) и выполняется при
  остановленной службе (PID-файл + тестовая `BEGIN IMMEDIATE` над
  рабочей базой), поэтому бот не увидит «половину» замены.

Восстановление при неудаче: рабочая база не изменяется до успешной
верификации; сама замена атомарна; прежняя база не удаляется, а
переименовывается в `<база>.replaced-<метка>` рядом (проверяемый след
очистки и точка восстановления). Бэкап фазы 2 — согласование WAL (Backup
API), полный независимый снимок на момент очистки.

Запуск (Makefile, каталог `Poster`):
    make db-maintain-report          # фазы 1–2: целостность + проверенный
                                     # WAL-бэкап + счётчики пользовательских
                                     # таблиц (read-only, без очистки)
    make db-maintain-clean           # полный цикл на рабочей БД (релизная
                                     # очистка, утверждена владельцем)
    make db-maintain-clean DB=path   # полный цикл на указанной базе

Проверка механизма выполняется ТОЛЬКО на временной базе:
    make db-maintain-clean DB=/tmp/opencode/poster_maint_test.db
Рабочая база в рамках настройки механизма не очищается.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from brick_types import SYSTEM_BRICK_CODES
from migrations import BUSY_TIMEOUT_MS, DEFAULT_MIGRATIONS, create_database_backup

APP_DIR = Path(__file__).resolve().parent
REAL_DB_PATH = APP_DIR / "post_bot.db"
# PID-файл службы (botctl.sh): rebuild меняет файл базы, поэтому обязан
# выполняться при остановленном боте. Каталог logs — рядом с модулем.
SERVICE_PID_FILE = APP_DIR / "logs" / "post_bot.pid"

# Журнал миграций: определяет совместимость схемы, чистке не подлежит.
SYSTEM_TABLES = ("schema_migrations",)


# Ошибка, которую выбрасывают все фазы при невозможности безопасно работать.
class MaintenanceError(RuntimeError):
    """Обслуживание прервано: база, бэкап или запись не прошли проверку."""


# --- Счётчики пользовательских таблиц ------------------------------------


def _table_names(connection: sqlite3.Connection) -> set[str]:
    """Имена всех таблиц базы (без view/индексов)."""
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _count_all_tables(
    connection: sqlite3.Connection, tables: "set[str] | list[str]"
) -> dict[str, int]:
    return {
        table: int(
            connection.execute(  # noqa: S608 — имя из sqlite_master
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
        )
        for table in sorted(tables)
    }


def user_table_counts(path: str | Path) -> dict[str, int]:
    """
    Счётчики строк всех пользовательских таблиц (read-only, для отчёта).

    Пользовательская таблица — любая таблица базы, кроме системного
    журнала миграций. Список берётся из фактической схемы базы, поэтому
    новая таблица миграций автоматически попадает в контроль нулевых
    счётчиков — ничего дублировать вручную не нужно. Используется фазой
    отчёта (`--skip-clean`) и верификацией rebuild: после очистки каждый
    счётчик обязан быть нулевым.
    """
    connection = _connect(path)
    try:
        return _count_all_tables(
            connection, _table_names(connection) - set(SYSTEM_TABLES)
        )
    finally:
        connection.close()


def _format_counts(counts: dict[str, int]) -> str:
    """Компактный текст счётчиков: только ненулевые, либо «все нулевые»."""
    nonzero = {table: count for table, count in counts.items() if count}
    if not nonzero:
        return "все пользовательские таблицы пусты"
    return json.dumps(nonzero, ensure_ascii=False, sort_keys=True)


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
# абсолютный путь базы. Очистка (фазы 3–6) отказывается работать без записи
# в этом множестве: бэкап обязателен.
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
    print(f"[2/6] Бэкап создан и проверен: {backup_path}")
    return backup_path


# --- Общие соединения и блокировки ---------------------------------------


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


# --- Фаза 3: служба остановлена ------------------------------------------


def ensure_service_stopped(db_path: str | Path) -> None:
    """
    Проверить, что служба Poster остановлена (rebuild меняет файл базы).

    Два независимых признака, оба обязательны:
    1. PID-файл `logs/post_bot.pid` (botctl.sh) отсутствует или процесс
       уже мёртв;
    2. над очищаемой базой берётся тестовая `BEGIN IMMEDIATE` (живой бот
       удерживает WAL-блокировки при записи; успешный захват подтверждает
       отсутствие активных писателей).

    Любой отказ прерывает rebuild до бэкапа фаз 4+ и любых изменений
    файлов. Замена файла базы под живым ботом дала бы рассинхрон кэшей
    SQLAlchemy и потерю WAL-хвоста.
    """
    if SERVICE_PID_FILE.exists():
        raw = SERVICE_PID_FILE.read_text(encoding="utf-8").strip()
        if raw.isdigit():
            pid = int(raw)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass  # процесс мёртв, файл устарел — не блокируем
            except OSError as exc:
                raise MaintenanceError(
                    f"Не удалось проверить PID {pid} службы Poster "
                    f"({SERVICE_PID_FILE}): {exc}. Остановите службу "
                    "командой ./botctl.sh stop."
                ) from exc
            else:
                raise MaintenanceError(
                    f"Служба Poster работает (PID {pid}, {SERVICE_PID_FILE}); "
                    "остановите её командой ./botctl.sh stop перед очисткой."
                )
    _guard_writable(db_path)
    print("[3/6] Служба остановлена: PID-файла нет, монопольный доступ есть.")


# --- Фаза 4: bootstrap новой базы штатным init_db ------------------------


def _run_init_db_subprocess(path: str | Path, *, timeout: int = 300) -> None:
    """init_db в отдельном процессе на указанной базе (как старт бота)."""
    env = os.environ.copy()
    env["DATABASE_URL"] = "sqlite:///" + str(path)
    env.setdefault(
        "TELEGRAM_BOT_TOKEN", "123456789:FIXTUREFAKETOKENforTestsOnly00000000"
    )
    result = subprocess.run(
        [sys.executable, "-c", "from database import init_db; init_db()"],
        cwd=str(APP_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise MaintenanceError(
            "Bootstrap init_db упал:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


def bootstrap_fresh_database(target_path: str | Path) -> None:
    """
    Создать новую базу штатным bootstrap в файле `target_path`.

    Никакого собственного DDL и никакого прямого доступа к данным: та же
    функция `init_db` (миграции 1..N + create_all + сидирование системных
    кирпичиков), что при старте бота. Новая база содержит ровно:
    актуальную схему, системные кирпичики, встроенный стандартный шаблон
    (его определения живут в коде `template_types.BUILTIN_TEMPLATE` и
    материализуются вместе с первым местом), конфигурацию (`.env`, в БД
    настроек нет) и ноль тестовых данных.
    """
    target = Path(target_path)
    if target.exists():
        raise MaintenanceError(
            f"Целевой файл bootstrap уже существует: {target} (не перезаписывается)."
        )
    _run_init_db_subprocess(target)
    if not target.exists() or target.stat().st_size == 0:
        raise MaintenanceError(f"Bootstrap не создал базу: {target}")


# --- Фаза 5: верификация rebuild-базы до замены --------------------------


def verify_rebuilt_database(
    path: str | Path,
    *,
    expected_version: int,
    system_brick_codes: tuple[str, ...],
) -> dict[str, object]:
    """
    Полная проверка новой базы ДО атомарной замены.

    Проверяется:
    - `PRAGMA integrity_check`;
    - журнал миграций: ровно версии 1..N без пропусков, где N —
      актуальная версия кода (`DEFAULT_MIGRATIONS`, не зашита);
    - каталог системных кирпичиков: активные строки `scope='system'`
      совпадают по кодам с `brick_types.SYSTEM_BRICKS` (источник —
      каталог кода продукта, число не зашито);
    - нулевые счётчики всех пользовательских таблиц.

    При любой неудаче — исключение: рабочая база не будет заменена.
    """
    connection = _connect(path)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
        integrity = str(row[0]) if row else "нет результата"
        if integrity.lower() != "ok":
            raise MaintenanceError(f"Новая база не прошла integrity_check: {integrity}")

        applied = [
            int(item[0])
            for item in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        if applied != list(range(1, expected_version + 1)):
            raise MaintenanceError(
                "Новая база содержит не полный журнал миграций: "
                f"{applied!r}, ожидалось 1..{expected_version}."
            )

        system_codes = [
            str(item[0])
            for item in connection.execute(
                "SELECT code FROM bricks WHERE scope = 'system' "
                "AND status = 'active' ORDER BY id"
            )
        ]
        if tuple(system_codes) != tuple(system_brick_codes):
            raise MaintenanceError(
                "Каталог системных кирпичиков новой базы не совпадает с "
                f"каталогом кода {system_brick_codes!r}: "
                f"получено {system_codes!r}."
            )

        counts = _count_all_tables(
            connection, _table_names(connection) - set(SYSTEM_TABLES)
        )
        # Системные кирпичики — НЕ тестовые данные: их счётчик сверяется с
        # каталогом кода выше и исключается из требования «нулевые».
        counts.pop("bricks", None)
        nonzero = {table: count for table, count in counts.items() if count}
        if nonzero:
            raise MaintenanceError(
                "Новая база содержит тестовые данные (ожидались нулевые "
                f"счётчики): {json.dumps(nonzero, ensure_ascii=False)}"
            )
    finally:
        connection.close()
    return {
        "integrity": integrity,
        "migrations": applied,
        "system_bricks": system_codes,
        "nonzero_counts": {},
    }


# --- Фаза 6: атомарная замена --------------------------------------------

# Прежняя база после успешной замены: сохраняется рядом для аудита и
# восстановления при неудаче (никогда не удаляется автоматически).
_REPLACED_SUFFIX = ".replaced-"
_WAL_SUFFIXES = ("-wal", "-shm")


def _utc_stamp() -> str:
    """Метки файлов: UTC, читаемая сортировка (как у бэкапов миграций)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _detach_sidecars(db_path: Path) -> None:
    """
    Убрать WAL-сателлиты очищаемой базы перед заменой файла.

    Живой WAL старой базы поверх новой после замены дал бы конфликт
    salt/страниц: SQLite мог бы признать его чужим (безопасно, но шумно)
    или применить устаревшие страницы (потеря новых данных). Порядок
    обеспечивает корректность:
    1. служба остановлена (фаза 3) и `_guard_writable` подтвердил, что
       активных писателей нет;
    2. тестовая транзакция + `PRAGMA wal_checkpoint(TRUNCATE)` выгружают
       содержимое WAL в основной файл и обрезают WAL до нуля;
    3. пустые `-wal`/`-shm` переименовываются рядом с теми же метками
       `.replaced-` — как и основной файл, они не удаляются.
    """
    connection = _connect(db_path)
    try:
        cursor = _writing_transaction(connection)
        if cursor is None:
            raise MaintenanceError(
                "База занята другим процессом: блокировка записи не взята."
            )
        connection.execute("ROLLBACK")
        # Checkpoint строго ВНЕ транзакции: активная транзакция на этом же
        # соединении блокирует TRUNCATE (readers-барьер SQLite).
        checkpointed = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpointed is None or int(checkpointed[0]) != 0:
            raise MaintenanceError(
                "wal_checkpoint(TRUNCATE) не выполнен: база занята другим "
                "процессом. Остановите службу и повторите очистку."
            )
    finally:
        connection.close()
    for suffix in _WAL_SUFFIXES:
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            os.replace(
                sidecar,
                sidecar.with_name(sidecar.name + _REPLACED_SUFFIX + _utc_stamp()),
            )


def _atomic_replace(old: Path, new: Path) -> Path:
    """
    Атомарная замена очищаемой базы новой: прежний файл сохраняется.

    `os.replace(new, old)` в одном каталоге атомарен. ПРЕЖНЯЯ база при
    этом не теряется: до замены она переименовывается в
    `<база>.replaced-<метка>` (та же атомарная операция rename), поэтому
    последовательность «rename старого → rename нового» не оставляет
    окна без файла базы: в любой момент по пути `old` существует либо
    старая, либо новая база. WAL-сателлиты старой базы убраны заранее
    (`_detach_sidecars`), у новой базы их нет: bootstrap-процесс закрыт.
    """
    replaced = old.with_name(old.name + _REPLACED_SUFFIX + _utc_stamp())
    os.replace(old, replaced)
    os.replace(new, old)
    return replaced


def clean_test_data(path: str | Path) -> dict[str, object]:
    """
    Полный rebuild-цикл очистки на указанной базе (фазы 3–6).

    Условие: для этой базы уже прошёл проверенный бэкап (backup_database).
    Рабочая база не изменяется до успешной верификации новой; замена
    атомарна, прежний файл сохраняется рядом (`.replaced-<метка>`).
    Возвращает отчёт фаз.
    """
    resolved = Path(path).resolve()
    if str(resolved) not in _VERIFIED_BACKUPS:
        raise MaintenanceError(
            "Очистка запрещена: для этой базы нет проверенного бэкапа. "
            "Сначала выполните фазу backup."
        )

    ensure_service_stopped(resolved)

    # Новая база — временный файл в том же каталоге (атомарный replace).
    staging = resolved.with_name(resolved.name + ".rebuild-" + _utc_stamp())
    try:
        bootstrap_fresh_database(staging)
        print("[4/6] Bootstrap новой базы завершён (init_db).")
        verification = verify_rebuilt_database(
            staging,
            expected_version=DEFAULT_MIGRATIONS[-1].version,
            system_brick_codes=SYSTEM_BRICK_CODES,
        )
        migrations_applied = verification["migrations"]
        brick_codes = verification["system_bricks"]
        assert isinstance(migrations_applied, list)  # узкий тип для отчёта
        assert isinstance(brick_codes, list)  # узкий тип для отчёта
        print(
            f"[5/6] Верификация новой базы: миграции 1..{len(migrations_applied)}, "
            f"системных кирпичиков {len(brick_codes)}, "
            "пользовательские счётчики нулевые."
        )
        _detach_sidecars(resolved)
        replaced = _atomic_replace(resolved, staging)
        print(f"[6/6] Атомарная замена выполнена. Прежняя база: {replaced}")
        return {
            "mode": "rebuild",
            "replaced_to": str(replaced),
            "migrations": migrations_applied,
            "system_bricks": brick_codes,
        }
    finally:
        # staging удалён заменой; при сбое до замены файл остаётся только
        # если bootstrap не успел его создать — удалять нечего. Файл,
        # созданный bootstrap, при сбое верификации удаляется здесь:
        # это артефакт этого запуска, не пользовательские данные.
        if staging.exists():
            try:
                staging.unlink()
            except OSError:
                pass


# --- Фаза verify (после замены) ------------------------------------------


def verify_after_clean(path: str | Path) -> str:
    """
    Поверка результата: integrity_check + smoke-запуск init_db.

    Отдельный процесс инициализирует базу как при старте бота (миграции
    должны быть no-op, схема — читаемой). Telegram не трогается: токен —
    фикстурный, init_db сети не касается.
    """
    resolved = Path(path).resolve()
    integrity = check_integrity(resolved)
    _run_init_db_subprocess(resolved, timeout=120)
    print(f"[6/6] Smoke-запуск init_db ok, integrity={integrity}.")
    return integrity


# --- Единая точка входа ---------------------------------------------------


def run_maintenance(path: str | Path, *, clean: bool = True) -> dict[str, object]:
    """
    Полный цикл обслуживания:
      report:  1) целостность, 2) проверенный WAL-бэкап + счётчики
               пользовательских таблиц (read-only);
      clean:   то же + 3) служба остановлена, 4) bootstrap новой базы,
               5) верификация, 6) атомарная замена + smoke-запуск.

    Рабочая база по умолчанию: обслуживание без `DB=` — это утверждённая
    владельцем релизная очистка тестовых данных Poster/post_bot.db.
    Проверка механизма выполняется только на временных базах с явным DB=.
    """
    resolved = Path(path).resolve()
    if not resolved.exists() or resolved.stat().st_size == 0:
        raise MaintenanceError(f"База не найдена или пуста: {resolved}")

    print(f"[1/6] База: {resolved}")
    integrity = check_integrity(resolved)
    print(f"[1/6] Целостность исходной базы: {integrity}")

    backup_path = backup_database(resolved)
    report: dict[str, object] = {"database": str(resolved), "backup": str(backup_path)}

    # Счётчики пользовательских сущностей — в обоих режимах: отчёт обязан
    # показывать остаток тестовых данных ДО очистки (задание 1).
    counts = user_table_counts(resolved)
    report["user_counts_before"] = counts
    print(f"[2/6] Счётчики пользовательских таблиц: {_format_counts(counts)}")

    if not clean:
        report["cleaned"] = False
        return report

    report.update(clean_test_data(resolved))

    # Пост-проверки результата (уже на новой базе): нулевые счётчики и
    # smoke-запуск init_db.
    counts_after = user_table_counts(resolved)
    report["user_counts_after"] = counts_after
    print("[6/6] Счётчики после очистки: " f"{_format_counts(counts_after)}")
    report["integrity_after"] = verify_after_clean(resolved)
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
        help="Только целостность, бэкап и счётчики, без очистки.",
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
