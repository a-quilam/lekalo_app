# report_store.py
"""
Постоянное хранение репортов пользователей (этап B, фундамент `/report`).

Репорт — жалоба автора на работу бота: текст, экран вызова и снимок
событий за последний час (`utils/user_events.py`). Строка переживает
рестарт службы: это обычная таблица, а не временный файл. UI и команда
`/report` — этап C; здесь только хранение и переходы статусов.

Правила слоя:

- `author_user_id` — постоянный Telegram user ID. `username`/`display_name`
  сохраняются как снимок на момент отправки и НЕ участвуют в авторизации;
- новый репорт всегда `active`; архив — только через `archive_report`;
- закрытие атомарно: `UPDATE ... WHERE status = 'active'`, второй вызов
  возвращает 0 и ничего не переписывает (двойное закрытие невозможно);
- `notified_user_ids` — JSON-список superadmin, кому уведомление уже
  доставлено. Запись атомарна (read-modify-write в одной транзакции):
  после рестарта повторной доставки не будет;
- функции сами управляют транзакцией (как остальные сторы).

Таблица создаётся базовой схемой 1.0.0 и описана только Core-таблицей: в
`models.py` её нет, чтобы `create_all` не создавал её в обход журнала
версий схемы.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    func,
    select,
    update,
)
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from models import utcnow

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"
REPORT_STATUSES = (STATUS_ACTIVE, STATUS_ARCHIVED)

# Размер страницы списка репортов: совпадает с общим размером списков
# Poster (utils/pagination.PAGE_SIZE). Пагинация — по 5, новые к старым.
REPORTS_PAGE_SIZE = 5

_METADATA = MetaData()

reports = Table(
    "reports",
    _METADATA,
    Column("id", Integer, primary_key=True),
    Column("author_user_id", Integer, nullable=False),
    Column("chat_id", Integer, nullable=False),
    Column("username", Text, nullable=True),
    Column("display_name", Text, nullable=True),
    Column("body", Text, nullable=False),
    Column("screen_label", Text, nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("events_snapshot", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("developer_reply", Text, nullable=True),
    Column("replied_at", DateTime, nullable=True),
    Column("closed_at", DateTime, nullable=True),
    Column("notified_user_ids", Text, nullable=True),
)


class ReportError(ValueError):
    """Репорт не может быть создан/закрыт: неверные данные."""


@dataclass(frozen=True)
class Report:
    """Снимок репорта для экрана и ответа разработчика."""

    id: int
    author_user_id: int
    chat_id: int
    username: str | None
    display_name: str | None
    body: str
    screen_label: str
    created_at: datetime
    events_snapshot: str
    status: str
    developer_reply: str | None
    replied_at: datetime | None
    closed_at: datetime | None
    notified_user_ids: tuple[int, ...]

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    @property
    def is_archived(self) -> bool:
        return self.status == STATUS_ARCHIVED

    def author_label(self) -> str:
        """Человекочитаемая подпись автора (username, имя или id)."""
        if self.username:
            return f"@{self.username}"
        if self.display_name:
            return self.display_name
        return str(self.author_user_id)


def _validated_id(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReportError(f"{field_name} должен быть целым числом")
    if value <= 0:
        raise ReportError(f"{field_name} должен быть положительным")
    return value


def _required_text(value: object, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ReportError(f"{field_name} не может быть пустым")
    return text


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _notified_from_json(raw: str | None) -> tuple[int, ...]:
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(data, list):
        return ()
    return tuple(int(item) for item in data if isinstance(item, int))


def _notified_to_json(values: tuple[int, ...]) -> str:
    return json.dumps(list(values))


def _view(row: RowMapping) -> Report:
    """Снимок репорта из строки результата запроса."""
    return Report(
        id=int(row["id"]),
        author_user_id=int(row["author_user_id"]),
        chat_id=int(row["chat_id"]),
        username=row["username"],
        display_name=row["display_name"],
        body=str(row["body"]),
        screen_label=str(row["screen_label"]),
        created_at=row["created_at"],
        events_snapshot=str(row["events_snapshot"]),
        status=str(row["status"]),
        developer_reply=row["developer_reply"],
        replied_at=row["replied_at"],
        closed_at=row["closed_at"],
        notified_user_ids=_notified_from_json(row["notified_user_ids"]),
    )


def _commit(session: Session) -> None:
    try:
        session.commit()
    except (IntegrityError, OperationalError) as exc:
        session.rollback()
        raise ReportError(
            "Не удалось сохранить репорт из-за одновременной записи. "
            "Повторите попытку."
        ) from exc


def create_report(
    session: Session,
    *,
    author_user_id: int,
    chat_id: int,
    body: str,
    screen_label: str,
    events_snapshot: str,
    username: str | None = None,
    display_name: str | None = None,
) -> Report:
    """
    Создаёт действующий (`active`) репорт и возвращает его снимок.

    `events_snapshot` — уже готовый текст снимка (.txt-содержимое): слой
    хранения не собирает его сам и не ходит в журнал событий.
    """
    checked_author = _validated_id(author_user_id, field_name="author_user_id")
    checked_chat = _validated_id(chat_id, field_name="chat_id")
    clean_body = _required_text(body, field_name="body")
    clean_screen = _required_text(screen_label, field_name="screen_label")
    snapshot = str(events_snapshot or "")
    try:
        result = session.execute(
            reports.insert()
            .values(
                author_user_id=checked_author,
                chat_id=checked_chat,
                username=_optional_text(username),
                display_name=_optional_text(display_name),
                body=clean_body,
                screen_label=clean_screen,
                created_at=utcnow(),
                events_snapshot=snapshot,
                status=STATUS_ACTIVE,
                notified_user_ids=None,
            )
            .returning(reports.c.id)
        )
        new_id = int(result.scalar_one())
        _commit(session)
    except Exception:
        session.rollback()
        raise
    created = get_report(session, new_id)
    assert created is not None  # строка только что вставлена в этой транзакции
    return created


def get_report(session: Session, report_id: int) -> Report | None:
    """Репорт по id; None — строки нет."""
    row = (
        session.execute(select(reports).where(reports.c.id == int(report_id)))
        .mappings()
        .first()
    )
    return _view(row) if row is not None else None


def _list_reports(
    session: Session,
    status: str,
    *,
    page: int = 0,
    page_size: int = REPORTS_PAGE_SIZE,
) -> tuple[list[Report], int]:
    """
    Страница репортов статуса, новые к старым, и общее число.

    `page` — 0-based; отрицательные значения и слишком большой номер
    безопасно приводятся к допустимым (пустая страница лучше исключения).
    """
    if status not in REPORT_STATUSES:
        raise ReportError(f"Неизвестный статус репорта: {status!r}")
    size = max(int(page_size), 1)
    base = select(reports).where(reports.c.status == status)
    total = int(session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    pages = (total + size - 1) // size if total else 0
    safe_page = min(max(int(page), 0), max(pages - 1, 0))
    rows = (
        session.execute(
            base.order_by(reports.c.created_at.desc(), reports.c.id.desc())
            .offset(safe_page * size)
            .limit(size)
        )
        .mappings()
        .all()
    )
    return [_view(row) for row in rows], total


def active_reports(
    session: Session, *, page: int = 0, page_size: int = REPORTS_PAGE_SIZE
) -> tuple[list[Report], int]:
    """Страница действующих репортов (новые к старым) и их общее число."""
    return _list_reports(session, STATUS_ACTIVE, page=page, page_size=page_size)


def archived_reports(
    session: Session, *, page: int = 0, page_size: int = REPORTS_PAGE_SIZE
) -> tuple[list[Report], int]:
    """Страница архивных репортов (новые к старым) и их общее число."""
    return _list_reports(session, STATUS_ARCHIVED, page=page, page_size=page_size)


def count_active(session: Session) -> int:
    """Сколько действующих репортов (счётчик на кнопке кабинета)."""
    return int(
        session.scalar(
            select(func.count())
            .select_from(reports)
            .where(reports.c.status == STATUS_ACTIVE)
        )
        or 0
    )


def archive_report(
    session: Session,
    report_id: int,
    *,
    reply: str | None = None,
    replied_at: datetime | None = None,
    closed_at: datetime | None = None,
) -> int:
    """
    Закрывает действующий репорт (ответ разработчика + архив).

    Возвращает число изменённых строк: 0 — репорт уже архивный или его нет.
    Второй вызов НЕ переписывает данные первого закрытия: условие
    `status = 'active'` проверяется в самом UPDATE, поэтому гонка двух
    закрытий тоже безопасна.
    """
    checked_id = _validated_id(report_id, field_name="report_id")
    moment = closed_at or utcnow()
    try:
        result = session.execute(
            update(reports)
            .where(
                reports.c.id == checked_id,
                reports.c.status == STATUS_ACTIVE,
            )
            .values(
                status=STATUS_ARCHIVED,
                developer_reply=_optional_text(reply),
                replied_at=replied_at or (moment if _optional_text(reply) else None),
                closed_at=moment,
            )
        )
        rowcount = int(getattr(result, "rowcount", 0) or 0)
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return rowcount


def save_reply(
    session: Session,
    report_id: int,
    *,
    reply: str,
    replied_at: datetime | None = None,
) -> int:
    """
    Сохраняет ответ разработчика, НЕ закрывая репорт.

    Тоже условный UPDATE по `status = 'active'`: архивный репорт не
    переписывается (0 изменённых строк).
    """
    checked_id = _validated_id(report_id, field_name="report_id")
    clean_reply = _required_text(reply, field_name="reply")
    try:
        result = session.execute(
            update(reports)
            .where(
                reports.c.id == checked_id,
                reports.c.status == STATUS_ACTIVE,
            )
            .values(
                developer_reply=clean_reply,
                replied_at=replied_at or utcnow(),
            )
        )
        rowcount = int(getattr(result, "rowcount", 0) or 0)
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return rowcount


def record_notification(
    session: Session, report_id: int, user_id: int
) -> tuple[int, ...]:
    """
    Добавляет superadmin в `notified_user_ids` и возвращает новый список.

    Идемпотентно: повторный вызов для того же user ID список не меняет.
    Запись идёт read-modify-write в одной транзакции; конкурентная доставка
    двумя обработчиками теоретически может потерять одну запись, поэтому
    фактическую защиту от повторов даёт проверка списка перед отправкой.
    """
    checked_id = _validated_id(report_id, field_name="report_id")
    checked_user = _validated_id(user_id, field_name="user_id")
    row = session.execute(
        select(reports.c.notified_user_ids).where(reports.c.id == checked_id)
    ).first()
    if row is None:
        raise ReportError(f"Репорт {checked_id} не найден")
    current = _notified_from_json(row[0])
    if checked_user in current:
        return current
    updated = current + (checked_user,)
    try:
        session.execute(
            update(reports)
            .where(reports.c.id == checked_id)
            .values(notified_user_ids=_notified_to_json(updated))
        )
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return updated
