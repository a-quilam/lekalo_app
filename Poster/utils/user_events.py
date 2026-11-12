# utils/user_events.py
"""
Структурированный журнал действий пользователя (этап B, фундамент репортов).

Зачем отдельная таблица, а не `logs/bot.log`: из журнала собирается
.txt-снимок «события пользователя N за последний час», который разработчик
получает вместе с жалобой. Обычный лог для этого не годится — он общий,
ротируется и не связан с Telegram user ID. `log_config` не дублируется:
там остаётся обычный лог процесса, здесь — события пользователя в БД.

Что пишется: только техническая навигация и исходы — `action`
(`screen.open`, `screen.back`, `field.edit.save`, `field.edit.cancel`,
`report.started`), `screen` (короткий код раздела), `scenario`, `result`
(`ok`/`error`/детали) и `detail` (безопасные технические параметры:
идентификаторы, счётчики, коды).

ЧЕГО ЗДЕСЬ НЕЛЬЗЯ ПИСАТЬ (жёсткое правило этапа):

- содержимое сообщений пользователя (текст анонса, жалобы, любые вводы);
- значения полей анонса (название, цена, описание, адрес и т.п.);
- токены, ключи, пароли и прочие секреты.

`detail` — короткая ОДНОСТРОЧНАЯ техническая строка. Помощник схлопывает
переводы строк и обрезает длину: многострочный текст не может попасть в
журнал целиком. Это защита от случайной ошибки, а не разрешение писать
пользовательский текст: контракт остаётся «только технические параметры»,
и его закрепляет тест.

Ошибка журнала НИКОГДА не ломает обработчик: `event_log` перехватывает
любое исключение БД, логирует его и возвращает `False`. Журнал —
диагностика, а не бизнес-операция, поэтому его сбой не должен срывать
сценарий пользователя.

Retention: строки старше `RETENTION_DAYS` удаляются по `created_at`.
Фоновых потоков нет — очистка запускается из самого `event_log` раз в
`CLEANUP_EVERY` записей (счётчик модуля, без таймеров и заданий PTB).

Таблица создаётся базовой схемой 1.0.0 и намеренно описана только
Core-таблицей: в `models.py` её нет, чтобы `create_all` не создавал её в
обход журнала версий схемы (как `announcement_instances` и
`brick_conflicts`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    delete,
    select,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from database import SessionLocal
from models import utcnow

logger = logging.getLogger(__name__)

# Фабрика сессии по умолчанию. Обработчики передают СВОЮ фабрику
# (`session_factory=SessionLocal` из своего модуля): интеграционные тесты
# подменяют `SessionLocal` в модулях-потребителях, и журнал обязан писать в
# ту же временную базу, а не в рабочую `post_bot.db`.
SessionFactory = Callable[[], Session]

# Сколько дней журнал хранит события. Период снимка для репорта — час, но
# небольшой запас нужен, чтобы разобрать жалобу, пришедшую с опозданием.
RETENTION_DAYS = 7

# Очистка раз в N записей: без фонового потока и без таймеров.
CLEANUP_EVERY = 200

# Пределы технических полей. `action`/`screen`/`scenario`/`result` — коды;
# `detail` — короткая диагностическая строка (не текст пользователя).
MAX_CODE_LENGTH = 64
MAX_RESULT_LENGTH = 200
MAX_DETAIL_LENGTH = 300

# Период снимка событий для репорта: последний час.
SNAPSHOT_PERIOD = timedelta(hours=1)

_METADATA = MetaData()

# Таблица уже существует (базовая схема 1.0.0): локальная metadata, к Base не
# подключена — `create_all` её не увидит.
user_event_log = Table(
    "user_event_log",
    _METADATA,
    Column("id", Integer, primary_key=True),
    Column("telegram_user_id", Integer, nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("action", Text, nullable=False),
    Column("screen", Text, nullable=True),
    Column("scenario", Text, nullable=True),
    Column("result", Text, nullable=True),
    Column("detail", Text, nullable=True),
)

# Счётчик записей до следующей очистки. Модульная переменная, а не строка
# в БД: при рестарте очистка просто произойдёт чуть позже — это диагностика,
# и точность расписания здесь не важна.
_events_since_cleanup = 0


@dataclass(frozen=True)
class UserEvent:
    """Одно событие журнала — снимок для сборки .txt-файла репорта."""

    id: int
    telegram_user_id: int
    created_at: datetime
    action: str
    screen: str | None
    scenario: str | None
    result: str | None
    detail: str | None

    def as_line(self) -> str:
        """
        Строка снимка: время, действие и технический контекст.

        Формат стабилен: его читает разработчик в .txt-файле репорта.
        """
        parts = [f"{self.created_at:%Y-%m-%d %H:%M:%S}", self.action]
        if self.screen:
            parts.append(f"screen={self.screen}")
        if self.scenario:
            parts.append(f"scenario={self.scenario}")
        if self.result:
            parts.append(f"result={self.result}")
        line = " | ".join(parts)
        if self.detail:
            line = f"{line} | {self.detail}"
        return line


def _code(value: str | None, limit: int) -> str | None:
    """Технический код: одна строка, обрезанная до предела, пустое → NULL."""
    if value is None:
        return None
    cleaned = " ".join(str(value).split())
    return cleaned[:limit] or None


# `_detail` ведёт себя так же, как `_code`: единственное отличие — предел
# длины. Смысловой проверки «это технические данные» здесь нет и быть не
# может — за неё отвечает вызывающий код (см. правило в docstring модуля),
# а тест фиксирует контракт хелпера на уровне поведения.
def _detail(value: str | None) -> str | None:
    """`detail` — одна строка, обрезанная до `MAX_DETAIL_LENGTH`."""
    return _code(value, MAX_DETAIL_LENGTH)


def event_log(
    telegram_user_id: int,
    action: str,
    *,
    screen: str | None = None,
    scenario: str | None = None,
    result: str | None = None,
    detail: str | None = None,
    session_factory: SessionFactory | None = None,
) -> bool:
    """
    Записывает техническое событие пользователя.

    Возвращает `True`, если строка записана, `False` — журнал недоступен
    (ошибка БД залогирована). Исключение наружу НЕ поднимается: журнал не
    должен ломать обработчик. Пустое/некорректное действие тоже даёт
    `False`, а не исключение.

    `session_factory` — фабрика сессии вызывающего модуля (по умолчанию
    `database.SessionLocal`). Передавать свою обязательно в обработчиках:
    тесты подменяют `SessionLocal` по модулям.
    """
    clean_action = _code(action, MAX_CODE_LENGTH)
    if not clean_action:
        logger.warning("Событие журнала без действия пропущено")
        return False
    factory = session_factory or SessionLocal
    try:
        with factory() as session:
            session.execute(
                user_event_log.insert().values(
                    telegram_user_id=int(telegram_user_id),
                    created_at=utcnow(),
                    action=clean_action,
                    screen=_code(screen, MAX_CODE_LENGTH),
                    scenario=_code(scenario, MAX_CODE_LENGTH),
                    result=_code(result, MAX_RESULT_LENGTH),
                    detail=_detail(detail),
                )
            )
            _maybe_cleanup(session)
            session.commit()
        return True
    except Exception as exc:  # noqa: BLE001 — журнал не должен ломать обработчик
        logger.error(
            "Не удалось записать событие пользователя %s (%s): %s",
            telegram_user_id,
            clean_action,
            exc,
        )
        return False


def list_user_events(
    telegram_user_id: int,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
    session_factory: SessionFactory | None = None,
) -> list[UserEvent]:
    """
    События пользователя за период, от старых к новым.

    По умолчанию — последний час (`SNAPSHOT_PERIOD`): именно этот интервал
    попадает в снимок репорта. Границы включительные: событие ровно час
    назад ещё попадает, более ранние — нет. Ошибка чтения возвращает пустой
    список: снимок без событий лучше, чем сорванный репорт.
    """
    if since is None:
        since = utcnow() - SNAPSHOT_PERIOD
    statement = select(user_event_log).where(
        user_event_log.c.telegram_user_id == int(telegram_user_id),
        user_event_log.c.created_at >= since,
    )
    if until is not None:
        statement = statement.where(user_event_log.c.created_at <= until)
    statement = statement.order_by(user_event_log.c.created_at, user_event_log.c.id)
    if limit is not None:
        statement = statement.limit(max(int(limit), 0))
    factory = session_factory or SessionLocal
    try:
        with factory() as session:
            rows = session.execute(statement).mappings().all()
    except Exception as exc:  # noqa: BLE001 — снимок не должен срывать репорт
        logger.error(
            "Не удалось прочитать события пользователя %s: %s",
            telegram_user_id,
            exc,
        )
        return []
    return [
        UserEvent(
            id=int(row["id"]),
            telegram_user_id=int(row["telegram_user_id"]),
            created_at=row["created_at"],
            action=str(row["action"]),
            screen=row["screen"],
            scenario=row["scenario"],
            result=row["result"],
            detail=row["detail"],
        )
        for row in rows
    ]


def build_events_snapshot(
    telegram_user_id: int,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    session_factory: SessionFactory | None = None,
) -> str:
    """
    Готовый .txt-снимок событий за период (по умолчанию — за час).

    Текст кладётся в `reports.events_snapshot` (этап C), а не во временный
    файл. Пустой журнал даёт короткую пояснительную строку, а не пустоту:
    разработчику важно видеть, что событий действительно не было.
    """
    events = list_user_events(
        telegram_user_id,
        since=since,
        until=until,
        session_factory=session_factory,
    )
    header = f"События пользователя {telegram_user_id}"
    if since is not None:
        header += f" с {since:%Y-%m-%d %H:%M:%S}"
    if until is not None:
        header += f" по {until:%Y-%m-%d %H:%M:%S}"
    if not events:
        return f"{header}\n\n(событий нет)"
    return header + "\n\n" + "\n".join(event.as_line() for event in events)


def cleanup_old_events(
    *, now: datetime | None = None, session_factory: SessionFactory | None = None
) -> int:
    """
    Удаляет события старше `RETENTION_DAYS`; возвращает число строк.

    Ошибка БД логируется, но не поднимается: retention — обслуживание, а
    не пользовательская операция.
    """
    moment = now or utcnow()
    threshold = moment - timedelta(days=RETENTION_DAYS)
    factory = session_factory or SessionLocal
    try:
        with factory() as session:
            result = session.execute(
                delete(user_event_log).where(user_event_log.c.created_at < threshold)
            )
            removed = int(getattr(result, "rowcount", 0) or 0)
            session.commit()
        return removed
    except Exception as exc:  # noqa: BLE001 — retention не ломает обработчик
        logger.error("Не удалось очистить старые события журнала: %s", exc)
        return 0


def _maybe_cleanup(session: Session) -> None:
    """
    Вероятностная очистка: раз в `CLEANUP_EVERY` записей.

    Вызывается внутри транзакции записи, поэтому удаление старых строк
    коммитится вместе с новым событием. Ошибка здесь не мешает записи:
    `event_log` всё равно закоммитит новое событие.
    """
    global _events_since_cleanup
    _events_since_cleanup += 1
    if _events_since_cleanup < CLEANUP_EVERY:
        return
    _events_since_cleanup = 0
    try:
        session.execute(
            delete(user_event_log).where(
                user_event_log.c.created_at < utcnow() - timedelta(days=RETENTION_DAYS)
            )
        )
    except SQLAlchemyError as exc:
        logger.error("Не удалось очистить старые события журнала: %s", exc)


def reset_cleanup_counter() -> None:
    """Сбрасывает счётчик очистки (тесты; в работе не нужен)."""
    global _events_since_cleanup
    _events_since_cleanup = 0
