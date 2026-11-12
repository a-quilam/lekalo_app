# database.py

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401 — регистрирует UserState/User/Place/PlaceName/...
from base import Base
from migrations import run_migrations

# .env может переопределить файл БД (в Docker: sqlite:////app/data/post_bot.db).
# Пустое значение или отсутствие переменной = путь по умолчанию.
load_dotenv()

# Путь по умолчанию привязан к каталогу ЭТОГО модуля (Poster/), а НЕ к текущей
# рабочей директории: запуск из корня репозитория, из IDE, из произвольной
# папки, launchd/systemd должен найти ту же рабочую базу, а не создавать
# пустой случайный post_bot.db рядом с рабочей директорией (архитектурный
# дефект Stage 12). Абсолютный путь получается на этапе импорта.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SQLITE_URL = "sqlite:///" + os.path.join(BASE_DIR, "post_bot.db")
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL") or DEFAULT_SQLITE_URL

# Busy timeout (сек): обработчики PTB и job-пул работают с БД из разных
# потоков/задач — ждём освобождения блокировки вместо мгновенного
# «database is locked».
SQLITE_BUSY_TIMEOUT_SECONDS = 30


def apply_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """
    PRAGMA на каждом новом соединении с SQLite:

    - busy_timeout — ждём блокировку (см. SQLITE_BUSY_TIMEOUT_SECONDS);
    - journal_mode=WAL — чтение не блокирует запись (параллельные job и
      обработчики), данные переживают сбои;
    - synchronous=NORMAL — рекомендуемый для WAL уровень надёжности/скорости;
    - foreign_keys=ON — внешние ключи (users/places/place_names, Stage 19A)
      проверяются настоящей SQLite, а не только кодом.

    Для не-SQLite БД функция не применяется. У SQLite-базы в памяти
    journal_mode остаётся memory (WAL недоступен), но busy_timeout и
    foreign_keys выставляются как обычно.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    # check_same_thread нужен только для SQLite (мультипоточные обработчики PTB)
    connect_args=(
        {"check_same_thread": False}
        if SQLALCHEMY_DATABASE_URL.startswith("sqlite")
        else {}
    ),
)
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    event.listen(engine, "connect", apply_sqlite_pragmas)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """
    Готовит базу к работе: базовая схема 1.0.0 → недостающие таблицы моделей.

    Порядок важен:
    1. `run_migrations` применяет ожидающие версии схемы. Для пустого файла
       это базовая версия 1.0.0: вся схема (32 таблицы с индексами, CHECK,
       FK и триггерами), журнал `schema_migrations` и каталог системных
       кирпичиков с финальными описаниями. На готовой базе ничего не
       применяется; перед первым ожидающим DDL существующей базы
       автоматически создаётся WAL-aware копия;
    2. `create_all` создаёт отсутствующие таблицы моделей — страховка на
       случай, если модель есть в коде, а в базовой схеме её нет. На полной
       базе это no-op: все таблицы уже созданы версией 1.0.0.

    Повторный запуск идемпотентен: журнал уже содержит версию 1, сидирование
    кирпичиков пропускается (`WHERE NOT EXISTS`), данные не переписываются.
    """
    run_migrations(engine)
    Base.metadata.create_all(bind=engine)
