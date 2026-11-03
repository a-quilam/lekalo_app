# announcement_instances.py
"""
Экземпляры готового анонса Poster 2.5 (§19–33, миграция 26).

Готовый анонс — цельное сообщение (или альбом + одна переиспользуемая
панель управления), а не экран «Анонс готов». Повторное открытие из «Моих
анонсов» отправляет НОВЫЙ экземпляр, прежний остаётся в чате архивным
снимком без кнопок. Поэтому экземпляр — отдельная сущность над логическим
черновиком `announcement_drafts`, а не расширение `AnnouncementDraft`:

- `active` — ровно один на черновик (частичный уникальный индекс);
- `archived` — неизменяемый снимок: без кнопок, не источник редакторов;
- `generation` — номер экземпляра в рамках черновика (max + 1);
- `message_ids` — JSON-массив в порядке отправки (альбом — все сообщения);
- `panel_message_id` — отдельная панель управления для медиагруппы; у
  одиночного сообщения кнопки на нём самом, поэтому NULL.

Модуль отвечает ТОЛЬКО за учёт экземпляров. Доставка остаётся у
`delivery.send_announcement_post`, а механизм прогресса — у
`announcement_store.publication_progress` (`begin`/`mark_sent`). Экземпляр
регистрируется ПОСЛЕ подтверждённой отправки (интеграция — этап C2), и
переписывать прогресс он не должен.

Таблица создаётся миграцией 26 и намеренно не добавлена в `models.py`:
`create_all` не должен воскрешать/создавать её в обход журнала миграций
(как `brick_conflicts` и `place_invite_links`). Поэтому здесь описана
только Core-таблица для запросов.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    Text,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from announcement_wizard import (
    AnnouncementConflictError,
    AnnouncementNotFoundError,
    AnnouncementValueError,
)
from models import AnnouncementDraft, utcnow

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"
INSTANCE_STATUSES = (STATUS_ACTIVE, STATUS_ARCHIVED)

# Таблица уже существует (миграция 26). `extend_existing` не нужен:
# metadata локальная, к Base она не подключена.
_METADATA = MetaData()

announcement_instances = Table(
    "announcement_instances",
    _METADATA,
    Column("id", Integer, primary_key=True),
    Column(
        "draft_id",
        Integer,
        ForeignKey("announcement_drafts.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("chat_id", Integer, nullable=False),
    Column("message_ids", Text, nullable=False),
    Column("panel_message_id", Integer, nullable=True),
    Column("generation", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("fingerprint", Text, nullable=True),
    Column("created_at", DateTime, nullable=False),
)


@dataclass(frozen=True)
class AnnouncementInstance:
    """Один отправленный экземпляр анонса (строке `announcement_instances`)."""

    id: int
    draft_id: int
    chat_id: int
    message_ids: tuple[int, ...]
    panel_message_id: int | None
    generation: int
    status: str
    fingerprint: str | None
    created_at: datetime

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    @property
    def is_archived(self) -> bool:
        return self.status == STATUS_ARCHIVED

    @property
    def has_panel(self) -> bool:
        """Кнопки живут на отдельной панели (медиагруппа), а не на сообщении."""
        return self.panel_message_id is not None


def _validated_id(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AnnouncementValueError(f"Некорректный {field_name} экземпляра анонса.")
    return value


def _validated_message_ids(values: object) -> tuple[int, ...]:
    if not isinstance(values, (list, tuple)):
        raise AnnouncementValueError("Адреса сообщений экземпляра должны быть списком.")
    result: list[int] = []
    for item in values:
        result.append(_validated_id(item, field_name="message_id"))
    if not result:
        raise AnnouncementValueError("Экземпляр анонса нельзя создать без message_id.")
    # Порядок отправки сохраняется, дубли (повторный Update) устраняются.
    return tuple(dict.fromkeys(result))


def _validated_optional_id(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    return _validated_id(value, field_name=field_name)


def _validated_fingerprint(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AnnouncementValueError("Отпечаток экземпляра анонса пуст.")
    return value.strip()


def _message_ids_from_json(raw: str | None) -> tuple[int, ...]:
    """
    Читает адреса сообщений; битое значение — явная ошибка, а не «пусто».

    Экземпляр без сообщений бессмысленен: молчаливое «нет адресов» скрыло
    бы повреждённые данные и позволило бы открыть битый редактор.
    """
    if not raw:
        raise AnnouncementValueError("У экземпляра анонса нет адресов сообщений.")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AnnouncementValueError(
            "Адреса сообщений экземпляра содержат битый JSON."
        ) from exc
    return _validated_message_ids(parsed)


def _message_ids_to_json(values: Sequence[int]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def _view(row: Any) -> AnnouncementInstance:
    return AnnouncementInstance(
        id=int(row.id),
        draft_id=int(row.draft_id),
        chat_id=int(row.chat_id),
        message_ids=_message_ids_from_json(row.message_ids),
        panel_message_id=(
            None if row.panel_message_id is None else int(row.panel_message_id)
        ),
        generation=int(row.generation),
        status=str(row.status),
        fingerprint=row.fingerprint,
        created_at=row.created_at,
    )


def _commit(session: Session) -> None:
    try:
        session.commit()
    except (IntegrityError, OperationalError) as exc:
        session.rollback()
        raise AnnouncementConflictError(
            "Не удалось сохранить экземпляр анонса из-за одновременной "
            "записи. Повторите попытку."
        ) from exc


def _require_draft(session: Session, draft_id: int) -> None:
    exists = session.execute(
        select(AnnouncementDraft.id).where(AnnouncementDraft.id == draft_id)
    ).first()
    if exists is None:
        raise AnnouncementNotFoundError("Анонс не найден.")


def _next_generation(session: Session, draft_id: int) -> int:
    current = session.execute(
        select(func.max(announcement_instances.c.generation)).where(
            announcement_instances.c.draft_id == draft_id
        )
    ).scalar()
    return (int(current) if current is not None else 0) + 1


def get_instance(session: Session, instance_id: int) -> AnnouncementInstance | None:
    """Экземпляр по id (callback `anp_...:{instance_id}`), включая архивный."""
    checked = _validated_id(instance_id, field_name="instance_id")
    row = session.execute(
        select(announcement_instances).where(announcement_instances.c.id == checked)
    ).first()
    return None if row is None else _view(row)


def get_active_instance(session: Session, draft_id: int) -> AnnouncementInstance | None:
    """Действующий экземпляр черновика (не более одного по индексу схемы)."""
    checked = _validated_id(draft_id, field_name="draft_id")
    row = session.execute(
        select(announcement_instances).where(
            announcement_instances.c.draft_id == checked,
            announcement_instances.c.status == STATUS_ACTIVE,
        )
    ).first()
    return None if row is None else _view(row)


def list_instances(session: Session, draft_id: int) -> list[AnnouncementInstance]:
    """Все экземпляры черновика: новые первыми (история §31)."""
    checked = _validated_id(draft_id, field_name="draft_id")
    rows = session.execute(
        select(announcement_instances)
        .where(announcement_instances.c.draft_id == checked)
        .order_by(
            announcement_instances.c.generation.desc(),
            announcement_instances.c.id.desc(),
        )
    ).all()
    return [_view(row) for row in rows]


def register_instance(
    session: Session,
    draft_id: int,
    chat_id: int,
    message_ids: Sequence[object],
    panel_message_id: int | None = None,
    fingerprint: str | None = None,
) -> AnnouncementInstance:
    """
    Регистрирует новый активный экземпляр после подтверждённой отправки.

    Прежний активный экземпляр сначала архивируется (§30): уникальный
    индекс схемы не допускает двух `active`, а прежние кнопки должны
    перестать работать. Всё — одна транзакция; при сбое не остаётся ни
    архива без нового экземпляра, ни двух действующих.
    """
    checked_draft = _validated_id(draft_id, field_name="draft_id")
    checked_chat = _validated_id(chat_id, field_name="chat_id")
    checked_messages = _validated_message_ids(message_ids)
    checked_panel = _validated_optional_id(
        panel_message_id, field_name="panel_message_id"
    )
    checked_fingerprint = _validated_fingerprint(fingerprint)
    try:
        _require_draft(session, checked_draft)
        _archive_active(session, checked_draft)
        new_id = session.execute(
            announcement_instances.insert()
            .values(
                draft_id=checked_draft,
                chat_id=checked_chat,
                message_ids=_message_ids_to_json(checked_messages),
                panel_message_id=checked_panel,
                generation=_next_generation(session, checked_draft),
                status=STATUS_ACTIVE,
                fingerprint=checked_fingerprint,
                created_at=utcnow(),
            )
            .returning(announcement_instances.c.id)
        ).scalar_one()
        _commit(session)
    except (IntegrityError, OperationalError) as exc:
        # Гонка двух регистраций или занятая база: уникальный индекс «один
        # active» не пропускает второй экземпляр, откат возвращает прежний.
        session.rollback()
        raise AnnouncementConflictError(
            "Не удалось зарегистрировать экземпляр анонса из-за "
            "одновременной записи. Повторите попытку."
        ) from exc
    except Exception:
        session.rollback()
        raise
    created = get_instance(session, int(new_id))
    assert created is not None  # строка только что вставлена в этой транзакции
    return created


def archive_instance(session: Session, instance_id: int) -> int:
    """
    Переводит экземпляр в архив (кнопки снимает вызывающий слой).

    Возвращает число изменённых строк: 0 — экземпляр уже архивный или его
    нет. Повторная архивация не ошибка: сценарий «снять управление» должен
    быть идемпотентным после рестарта.
    """
    checked = _validated_id(instance_id, field_name="instance_id")
    try:
        result = session.execute(
            update(announcement_instances)
            .where(
                announcement_instances.c.id == checked,
                announcement_instances.c.status == STATUS_ACTIVE,
            )
            .values(status=STATUS_ARCHIVED)
        )
        rowcount = int(getattr(result, "rowcount", 0) or 0)
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return rowcount


def replace_instance(
    session: Session,
    old_id: int,
    new_message_ids: Sequence[object],
    new_panel_message_id: int | None = None,
    fingerprint: str | None = None,
) -> AnnouncementInstance:
    """
    Атомарная замена активного экземпляра (смена структуры медиа, §28).

    Старый экземпляр уходит в архив, новый становится активным — в ОДНОЙ
    транзакции. После вызова на черновике ровно один `active`. Чат, как и
    черновик, наследуются от старого экземпляра: замена происходит в том же
    чате, где было управление.
    """
    previous = get_instance(session, _validated_id(old_id, field_name="instance_id"))
    if previous is None:
        raise AnnouncementNotFoundError("Экземпляр анонса не найден.")
    if previous.is_archived:
        raise AnnouncementValueError(
            "Заменить можно только действующий экземпляр анонса."
        )
    checked_messages = _validated_message_ids(new_message_ids)
    checked_panel = _validated_optional_id(
        new_panel_message_id, field_name="panel_message_id"
    )
    checked_fingerprint = _validated_fingerprint(fingerprint)
    try:
        session.execute(
            update(announcement_instances)
            .where(
                announcement_instances.c.id == previous.id,
                announcement_instances.c.status == STATUS_ACTIVE,
            )
            .values(status=STATUS_ARCHIVED)
        )
        new_id = session.execute(
            announcement_instances.insert()
            .values(
                draft_id=previous.draft_id,
                chat_id=previous.chat_id,
                message_ids=_message_ids_to_json(checked_messages),
                panel_message_id=checked_panel,
                generation=_next_generation(session, previous.draft_id),
                status=STATUS_ACTIVE,
                fingerprint=(
                    checked_fingerprint
                    if checked_fingerprint is not None
                    else previous.fingerprint
                ),
                created_at=utcnow(),
            )
            .returning(announcement_instances.c.id)
        ).scalar_one()
        _commit(session)
    except (IntegrityError, OperationalError) as exc:
        session.rollback()
        raise AnnouncementConflictError(
            "Не удалось заменить экземпляр анонса из-за одновременной "
            "записи. Повторите попытку."
        ) from exc
    except Exception:
        session.rollback()
        raise
    created = get_instance(session, int(new_id))
    assert created is not None
    return created


def set_panel_message(
    session: Session, instance_id: int, panel_message_id: int | None
) -> int:
    """
    Записывает адрес отдельной панели управления после её отправки.

    Панель отправляется ОТДЕЛЬНОЙ операцией после доставки контента (§22),
    поэтому её id не может прийти вместе с `message_ids`: экземпляр уже
    зарегистрирован, и остаётся только дописать адрес. Возвращает число
    изменённых строк (0 — экземпляра нет).
    """
    checked = _validated_id(instance_id, field_name="instance_id")
    checked_panel = _validated_optional_id(
        panel_message_id, field_name="panel_message_id"
    )
    try:
        result = session.execute(
            update(announcement_instances)
            .where(announcement_instances.c.id == checked)
            .values(panel_message_id=checked_panel)
        )
        rowcount = int(getattr(result, "rowcount", 0) or 0)
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return rowcount


def strip_panel(session: Session, instance_id: int) -> int:
    """
    Снимает отдельную панель управления (кнопки переехали на сообщение).

    `panel_message_id = NULL`; адреса самого анонса не меняются. Возвращает
    число изменённых строк (0 — панели не было или экземпляра нет).
    """
    checked = _validated_id(instance_id, field_name="instance_id")
    try:
        result = session.execute(
            update(announcement_instances)
            .where(
                announcement_instances.c.id == checked,
                announcement_instances.c.panel_message_id.is_not(None),
            )
            .values(panel_message_id=None)
        )
        rowcount = int(getattr(result, "rowcount", 0) or 0)
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return rowcount


def delete_instances(session: Session, draft_id: int) -> int:
    """
    Удаляет историю экземпляров черновика (удаление анонса §33).

    Сообщения в Telegram при этом не трогаются: удаляется только локальная
    запись, управление без неё становится устаревшим callback. FK `CASCADE`
    схемы делает то же при удалении черновика; функция нужна для явного
    пути и проверки числа строк.
    """
    checked = _validated_id(draft_id, field_name="draft_id")
    try:
        result = session.execute(
            delete(announcement_instances).where(
                announcement_instances.c.draft_id == checked
            )
        )
        rowcount = int(getattr(result, "rowcount", 0) or 0)
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return rowcount


def _archive_active(session: Session, draft_id: int) -> int:
    """Архивирует действующий экземпляр черновика (внутри транзакции)."""
    result = session.execute(
        update(announcement_instances)
        .where(
            announcement_instances.c.draft_id == draft_id,
            announcement_instances.c.status == STATUS_ACTIVE,
        )
        .values(status=STATUS_ARCHIVED)
    )
    return int(getattr(result, "rowcount", 0) or 0)
