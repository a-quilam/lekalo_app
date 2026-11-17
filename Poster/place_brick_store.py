# place_brick_store.py
"""
Локальные описания системных кирпичиков для одного места (Poster 2.6.2 §3.2).

Зачем отдельный слой: каталог `bricks` общий, описание системного кирпичика
правит владелец системы. Площадке же нужно своё пояснение для автора анонса.
Полное копирование системных кирпичиков в место ЗАПРЕЩЕНО — хранится только
переопределение текста описания, пара (`place_id`, `brick_id`) уникальна.

Правила:

- читать и писать может только управляющий ЭТИМ местом (или суперадминистратор) —
  права проверяются по фактическим строкам БД, а не по факту прихода в экран;
- переопределять можно только АКТИВНЫЙ системный кирпичик живого каталога;
- `revision` увеличивается при каждой успешной правке (оптимистическая
  блокировка экрана, как у `bricks`/`templates`);
- `reset_*` удаляет строку — карточка и мастер снова видят глобальное
  описание каталога.

Схема `place_brick_descriptions` создана миграцией `place_photos_2_6_2`; этот
модуль её не меняет.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeGuard

from sqlalchemy import delete, select
from sqlalchemy import update as sqlalchemy_update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from brick_store import (
    BrickDefinition,
    BrickNotFoundError,
    BrickPermissionError,
    get_visible_brick,
    list_system_bricks,
)
from brick_types import BRICK_SCOPE_SYSTEM, BRICK_STATUS_ACTIVE, clean_brick_description
from models import Brick, Place, PlaceBrickDescription, User, utcnow
from permissions import can_manage_place


class PlaceBrickDescriptionError(ValueError):
    """Текст описания не может быть сохранён (пустой или не строка)."""


@dataclass(frozen=True)
class PlaceBrickView:
    """
    Системный кирпичик глазами конкретного места.

    `description` — ДЕЙСТВУЮЩЕЕ описание: локальное переопределение площадки,
    если оно есть, иначе глобальное описание каталога. `local_description`
    отдельно нужно карточке, чтобы показать источник и кнопку возврата.
    """

    brick: BrickDefinition
    description: str | None
    local_description: str | None
    revision: int | None

    @property
    def is_local(self) -> bool:
        return self.local_description is not None


@dataclass(frozen=True)
class PlaceBrickMutation:
    """Результат записи локального описания: понятный статус без исключений."""

    status: str
    description: str | None = None
    message: str | None = None


def _valid_place_id(place_id: object) -> TypeGuard[int]:
    return not isinstance(place_id, bool) and isinstance(place_id, int) and place_id > 0


def _valid_brick_id(brick_id: object) -> TypeGuard[int]:
    return not isinstance(brick_id, bool) and isinstance(brick_id, int) and brick_id > 0


def _require_place(session: Session, place_id: object) -> Place:
    if not _valid_place_id(place_id):
        raise BrickNotFoundError("Некорректный идентификатор места.")
    place = session.get(Place, place_id)
    if place is None:
        raise BrickNotFoundError("Место не найдено.")
    return place


def _require_actor(session: Session, telegram_user_id: object) -> User:
    if (
        isinstance(telegram_user_id, bool)
        or not isinstance(telegram_user_id, int)
        or telegram_user_id <= 0
    ):
        raise BrickPermissionError("Пользователь не определён.")
    actor = session.scalar(
        select(User).where(User.telegram_user_id == telegram_user_id)
    )
    if actor is None:
        raise BrickPermissionError(
            "Пользователь ещё не зарегистрирован в таблице пользователей."
        )
    return actor


def _require_managed_place(session: Session, actor: User, place_id: object) -> Place:
    place = _require_place(session, place_id)
    if not can_manage_place(session, actor.telegram_user_id, place.id):
        raise BrickPermissionError(
            "Описание кирпичика для этой площадки меняет только её "
            "администратор или суперадминистратор."
        )
    return place


def _read_row(
    session: Session, place_id: int, brick_id: int
) -> PlaceBrickDescription | None:
    return session.scalar(
        select(PlaceBrickDescription).where(
            PlaceBrickDescription.place_id == place_id,
            PlaceBrickDescription.brick_id == brick_id,
        )
    )


# --- Чтение -----------------------------------------------------------------


def place_brick_overrides(session: Session, place_id: int) -> dict[int, str]:
    """Все локальные описания места: `brick_id` → текст."""
    if not _valid_place_id(place_id):
        return {}
    rows = session.scalars(
        select(PlaceBrickDescription).where(PlaceBrickDescription.place_id == place_id)
    ).all()
    return {row.brick_id: row.description for row in rows}


def effective_description(
    session: Session, place_id: int | None, definition: BrickDefinition
) -> str | None:
    """
    Действующее описание кирпичика для места.

    Переопределение учитывается только у системного кирпичика: локальные и
    личные описания живут в своих областях и подменять их нельзя.
    """
    if (
        definition.scope != BRICK_SCOPE_SYSTEM
        or not _valid_place_id(place_id)
        or not _valid_brick_id(definition.id)
    ):
        return definition.description
    row = _read_row(session, place_id, definition.id)
    return row.description if row is not None else definition.description


def get_place_brick_view(
    session: Session,
    place_id: int,
    brick_id: int,
    *,
    include_archived: bool = False,
) -> PlaceBrickView | None:
    """
    Системный кирпичик в контексте места; `None` — не системный/не виден.

    Чужой или локальный кирпичик сюда не попадает: экран места работает
    только с живым системным каталогом.
    """
    if not _valid_place_id(place_id) or not _valid_brick_id(brick_id):
        return None
    definition = get_visible_brick(
        session,
        brick_id,
        viewer_telegram_user_id=None,
        place_id=place_id,
        include_archived=include_archived,
    )
    if definition is None or definition.scope != BRICK_SCOPE_SYSTEM:
        return None
    row = _read_row(session, place_id, brick_id)
    local = row.description if row is not None else None
    return PlaceBrickView(
        brick=definition,
        description=local if local is not None else definition.description,
        local_description=local,
        revision=row.revision if row is not None else None,
    )


def list_place_system_brick_views(
    session: Session, place_id: int, *, include_archived: bool = False
) -> list[PlaceBrickView]:
    """
    Живой системный каталог с действующими описаниями места.

    Список берётся из каталога (`list_system_bricks`), а не из хардкода:
    новый системный кирпичик появляется на экране без правки кода.
    """
    if not _valid_place_id(place_id):
        return []
    overrides = place_brick_overrides(session, place_id)
    views: list[PlaceBrickView] = []
    for definition in list_system_bricks(session, include_archived=include_archived):
        local = overrides.get(definition.id)
        views.append(
            PlaceBrickView(
                brick=definition,
                description=local if local is not None else definition.description,
                local_description=local,
                revision=None,
            )
        )
    return views


# --- Запись -----------------------------------------------------------------


def _system_brick_for_write(session: Session, brick_id: int) -> Brick:
    if not _valid_brick_id(brick_id):
        raise BrickNotFoundError("Кирпичик не найден.")
    row = session.get(Brick, brick_id)
    if row is None:
        raise BrickNotFoundError("Кирпичик не найден или уже удалён.")
    if row.scope != BRICK_SCOPE_SYSTEM:
        # Локальные и личные кирпичики сюда не относятся: их описание живёт в
        # своей области, подменять его экраном места нельзя.
        raise BrickNotFoundError("Кирпичик не найден или больше недоступен.")
    return row


def _clean(raw: object | None) -> str | None:
    try:
        return clean_brick_description(raw)
    except ValueError as exc:  # пустой/нестроковый текст
        raise PlaceBrickDescriptionError(str(exc)) from exc


def set_place_brick_description(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
    brick_id: int,
    description: object | None = None,
) -> PlaceBrickMutation:
    """
    Сохраняет ЛОКАЛЬНОЕ описание места; каталог и другие места не меняются.

    Пустой текст трактуется как «вернуть стандартное описание»: строка
    удаляется, карточка снова показывает глобальное описание кирпичика.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    place = _require_managed_place(session, actor, place_id)
    brick = _system_brick_for_write(session, brick_id)
    if brick.status != BRICK_STATUS_ACTIVE:
        return PlaceBrickMutation(
            "archived",
            message="Кирпичик в архиве: описание для площадки не сохраняется.",
        )
    cleaned = _clean(description)
    if cleaned is None:
        return reset_place_brick_description(
            session,
            actor_telegram_user_id=actor_telegram_user_id,
            place_id=place.id,
            brick_id=brick.id,
        )
    row = _read_row(session, place.id, brick.id)
    if row is not None and row.description == cleaned:
        return PlaceBrickMutation(
            "unchanged",
            description=cleaned,
            message="Описание не изменилось.",
        )
    try:
        if row is None:
            session.add(
                PlaceBrickDescription(
                    place_id=place.id,
                    brick_id=brick.id,
                    description=cleaned,
                    revision=1,
                    updated_at=utcnow(),
                    updated_by_user_id=actor.id,
                )
            )
        else:
            changed = session.execute(
                sqlalchemy_update(PlaceBrickDescription)
                .where(
                    PlaceBrickDescription.id == row.id,
                    PlaceBrickDescription.revision == row.revision,
                )
                .values(
                    description=cleaned,
                    revision=row.revision + 1,
                    updated_at=utcnow(),
                    updated_by_user_id=actor.id,
                )
                .execution_options(synchronize_session=False)
            )
            if getattr(changed, "rowcount", 0) != 1:
                session.rollback()
                return PlaceBrickMutation(
                    "stale",
                    message="Описание уже изменилось в другом окне. Откройте заново.",
                )
        session.commit()
    except (IntegrityError, OperationalError):
        session.rollback()
        return PlaceBrickMutation(
            "conflict",
            message="Не удалось сохранить описание из-за ошибки базы. "
            "Повторите попытку.",
        )
    except Exception:
        session.rollback()
        raise
    return PlaceBrickMutation(
        "updated", description=cleaned, message="Описание для площадки сохранено."
    )


def reset_place_brick_description(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
    brick_id: int,
) -> PlaceBrickMutation:
    """Удаляет локальное переопределение: снова действует описание каталога."""
    actor = _require_actor(session, actor_telegram_user_id)
    place = _require_managed_place(session, actor, place_id)
    brick = _system_brick_for_write(session, brick_id)
    row = _read_row(session, place.id, brick.id)
    if row is None:
        return PlaceBrickMutation(
            "no_override",
            description=brick.description,
            message="У этой площадки своё описание не задано.",
        )
    try:
        session.execute(
            delete(PlaceBrickDescription).where(
                PlaceBrickDescription.id == row.id,
                PlaceBrickDescription.revision == row.revision,
            )
        )
        session.commit()
    except (IntegrityError, OperationalError):
        session.rollback()
        return PlaceBrickMutation(
            "conflict",
            description=brick.description,
            message="Не удалось вернуть общее описание из-за ошибки базы. "
            "Повторите попытку.",
        )
    except Exception:
        session.rollback()
        raise
    return PlaceBrickMutation(
        "reset",
        description=brick.description,
        message="Возвращено общее описание каталога.",
    )


def place_brick_description_map(session: Session, place_id: Any) -> dict[int, str]:
    """
    Карта переопределений для конструктора анонса: `brick_id` → текст.

    Отдельная функция нужна мастеру: он строит поля одного черновика и
    подставляет локальное описание вместо глобального.
    """
    if not _valid_place_id(place_id):
        return {}
    return place_brick_overrides(session, place_id)
